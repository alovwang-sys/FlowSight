# Task: Prepare Exact Sidecar Runtime Configuration

## Task Metadata

```yaml
task_id: P0-014
release: v1
task_type: implementation
status: in_progress
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

Normalize one project root into an immutable, scalar-only, repr-safe internal
sidecar configuration before any child handoff or process launch.

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
  `prepare_sidecar_runtime_config(project_root: str | Path, *, requested_port: int | None = None, startup_timeout: float = 5.0) -> SidecarRuntimeConfig`.
- This task does not change `FlowSight`. A later SDK lifecycle task will map its
  existing `project_root` and `ui_port` inputs to this already-reviewed factory.
- Codex primary, acting as task owner, approved the post-activation
  clarification that fixed `__reduce__`, `__reduce_ex__`, and `__getstate__`
  rejection guards enforce the existing no-serializer boundary. They create no
  wire format or alternate instance and change no goal, allowed file, phase, or
  scope override.
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
  inert P0-001 `StateStore` path normalization are permitted. The frozen
  platformdirs query and its standard-library fallback may perform their own
  environment, access, and temporary-directory availability probes; those
  probes are not FlowSight product state. It must still run with
  `ensure_exists=False` and create or mutate no FlowSight project/runtime target
  or descendant.
- Do not inspect incumbent state, decide requested-port compatibility, bind a
  listener, elect/wait for an owner, adopt/close a descriptor, or manufacture a
  startup state.
- Do not define a wire format, environment or command, startup channel, child
  handoff, `pass_fds`, subprocess, process polling/cleanup, ASGI/Uvicorn
  runtime, or READY protocol.
- Do not add SQLite/writer/queue/lease/SDK/OTel/reload/shutdown/UI/browser
  behavior or import spike code. Apart from the exact platformdirs pin already
  selected by this task from the MVP's library choice, do not add, remove, or
  change another dependency.
- Apart from the five declared exact scalar fields on a successful
  `SidecarRuntimeConfig`, do not expose a token, database/state/lock path, PID,
  startup ID, final port, deadline, descriptor, process handle, raw exception,
  caller path/value, or runtime path in repr, errors, logs, stdout/stderr,
  callbacks, caches, or side results. The configuration fields themselves must
  never be interpolated into repr or an error. An identity-preserved
  non-`Exception` `BaseException` may retain its caller-supplied payload;
  production must not inspect, format, print, log, cache, or retain it and must
  perform no later work.
  A caller's already-active Python-managed exception context is not an internal
  disclosure and may remain as described in the acceptance criteria.
- Do not add a default-port override, public mutable field, public constructor,
  `to_wire`/`from_wire`, serializer, callback, mutable module state, or cache.

## Acceptance Criteria

- [ ] `SidecarRuntimeConfig` is an exact frozen, slot-only value object with
  exactly `project_root: str`, `runtime_root: str`, `project_id: str`,
  `requested_port: int | None`, and `startup_timeout: float`. Direct
  construction with any positional/keyword argument shape always raises the
  fixed context-free
  `TypeError("SidecarRuntimeConfig must be prepared")`; repr is exactly
  `<SidecarRuntimeConfig>` and exposes no field.
- [ ] `SidecarRuntimeConfig` and `prepare_sidecar_runtime_config` are identical
  `flowsight.sidecar` exports and each occurs exactly once in `__all__`. The
  preparation function has the exact fixed signature and adds no alternate
  constructor, wire helper, public result wrapper, or extra field. Copy,
  deepcopy, and pickle serialization fail before producing a side result with
  exact context-free `TypeError("SidecarRuntimeConfig cannot be serialized")`;
  the three fixed rejection guards are not serializers.
- [ ] `project_root` accepts only an exact built-in `str` or exact platform
  `Path`. It resolves strictly to an existing directory other than the
  filesystem root; relative, absolute, and symlink aliases of one directory
  produce the same exact canonical string and project ID. Before resolution,
  the exact input's built-in string form, and after resolution the canonical
  string, must each be nonempty, control-free, at most 4096 characters, and
  `os.fsencode` to exact built-in bytes of at most 4096 bytes. Here and for the
  runtime root, control-free means every character has `ord(character) >= 32`
  and `ord(character) != 127`. A wrong input type raises exactly context-free
  `TypeError("project_root must be an exact built-in str or platform Path")`.
  Empty/NUL/control text, overlong roots, missing paths, files, filesystem roots
  (including alternate or symlink aliases), and ordinary resolution failures
  raise exactly context-free
  `ValueError("project_root is invalid")`; process-control errors preserve
  identity.
- [ ] `project_id` is exactly `"project-v1-" +
  sha256(b"flowsight-project-v1\x00" +
  os.fsencode(canonical_root)).hexdigest()`, with 64 lowercase hexadecimal
  digest characters after the prefix. It contains no raw path and fixed
  distinct-root fixtures produce their independently expected different digest
  identities. At least one test fixes the expected hexadecimal digest
  independently rather than recomputing it through a production helper.
- [ ] Production captures `platformdirs.user_runtime_path` at import and calls
  it exactly once as `user_runtime_path("flowsight", appauthor=False,
  ensure_exists=False)`. `pyproject.toml` has one exact unmarked runtime pin
  `platformdirs==4.10.0` and no duplicate dev pin. Public attribute replacement
  cannot change direct dispatch; a private seam remains fault injection only.
- [ ] The platformdirs result must be an exact platform `Path` that is already
  absolute, is not the filesystem root, has no `..` lexical component, and
  encodes as nonempty, control-free text of at most 4096 characters and exact
  built-in bytes of at most 4096 bytes. A relative, root, derived, malformed, or
  ordinary failed result raises the fixed runtime configuration error with zero
  `StateStore` construction or later work.
- [ ] The exact platformdirs `Path` is passed to one canonical P0-001
  `StateStore` construction with the derived project ID and must return an
  exact `StateStore`. Production reads only that exact instance dictionary
  through a built-in operation; the `runtime_root` field must be an exact
  platform `Path`, with no attribute or `__fspath__` dispatch on a malformed
  value. The config stores its normalized absolute, non-root, `..`-free root as
  a nonempty, control-free, at-most-4096-character and 4096-fsencoded-byte
  built-in string, never its project-specific `runtime_dir`. No FlowSight
  project/runtime target or descendant is created or mutated, and no store or
  `Path` identity is retained. Malformed/ordinary collaborator failure becomes fixed
  `RuntimeError("sidecar runtime configuration failed")`. This task trusts the
  canonical constructor's project-path derivations and validates only the
  exact result type, exact matching `project_id`, and exact lexical
  `runtime_root`; it neither inspects nor recomputes runtime-dir, state, lock,
  mutation-lock, or database paths. Production captures the canonical
  `StateStore` constructor at import; public attribute replacement cannot
  redirect it, while a private seam remains fault injection rather than a
  provenance boundary.
- [ ] `requested_port` accepts only `None` or an exact built-in `int` in
  `0..65535`; `bool`, subclasses, coercible values, and out-of-range values fail
  exactly with context-free `ValueError` whose message is
  `requested_port must be None or an exact built-in int in 0..65535`. `None`,
  `0`, and explicit ports remain distinct scalar values; this task performs no
  bind or compatibility policy.
- [ ] `startup_timeout` accepts only exact built-in `int`/`float` values that
  are finite, positive, and at most 30 seconds, then stores one exact `float`.
  A wrong type raises exactly context-free
  `TypeError("startup_timeout must be a built-in int or float")`; invalid
  numeric values raise exactly context-free
  `ValueError("startup_timeout must be finite, positive, and at most 30 seconds")`.
  It reads no clock and creates no deadline. Validation order is project-root
  top-level type, port, timeout, project-root text/resolution, project-ID
  derivation, platformdirs, `StateStore`, then private construction. No invalid
  prefix performs later work.
- [ ] Success contains only exact scalar values, is value-equal for equal
  inputs, remains unchanged after caller filesystem aliases change, and retains
  no caller `Path`, platformdirs `Path`, `StateStore`, callback, or raw
  internally caught exception. Fixed failures are created and raised `from
  None` only after leaving an internal `except` suite; absent a caller-active
  exception their cause, context, and notes are empty. A caller's already-active
  Python-managed context may remain, but formatted output suppresses it and
  production never reads or caches it. Repr/errors/stdout/stderr expose no
  project or runtime path except that an identity-preserved non-`Exception`
  `BaseException` may still carry its preexisting caller payload; production
  never formats or emits that payload.
- [ ] A `KeyboardInterrupt`, `SystemExit`, or other non-`Exception`
  `BaseException` from project-root resolution, digest encoding/hashing,
  platformdirs, `StateStore` construction, exact dictionary/root inspection, or
  private construction preserves identity and ends the operation with no later
  work. Ordinary failures alone are normalized to the fixed errors above.
- [ ] Deterministic tests cover public shape, direct construction, frozen/slots,
  fixed repr/errors, root type/existence/directory/alias matrices, exact digest,
  platformdirs call/provenance/failure, no FlowSight target mutation, normalized
  StateStore root versus runtime-dir confusion, complete port/timeout
  boundaries, exact float normalization, process-control at every seam,
  ordering/no-later-work, equality, non-retention, target bytes/metadata
  unchanged, and stdout/stderr privacy.
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

- Supported relative, absolute, and symlink aliases of one project root must
  not create duplicate sidecars or source-sandbox identities.
- Canonical strings establish lexical identity only. This slice does not unify
  case-only spellings on case-insensitive volumes, hard-link aliases, or bind
  mounts, and it does not prove inode continuity or eliminate TOCTOU. Phase 2
  source access must still enforce realpath/descriptor containment for every
  read.
- Passing `runtime_dir` instead of `runtime_root` to a child would make
  `StateStore` add a second `project-<digest>` layer and split ownership.
- Treating `bool`, a derived integer, `None`, or port `0` as interchangeable can
  silently change future listener policy.
- A repr, exception, retained input, or premature wire helper can leak a local
  path or freeze an unreviewed child protocol.
- This cooperative timeout is only a future launch admission duration; it is
  not a hard wall-clock process interrupt and this task reads no clock.

## Reviewer Focus

- Can a supported relative/absolute/symlink alias, platformdirs failure, a
  malformed injected result, coercion, or malformed path create a different
  successful project/runtime identity?
- Does any output or retained object expose a path or caller-controlled value?
- Did the task stop before handoff, process, listener, election, state mutation,
  requested-port policy, SDK lifecycle, source scanning, and wire format?
- Are the five scalars sufficient for the next child-handoff task without
  inventing its versioned protocol here?

## Role Outputs

Implementer:
- Codex primary implemented one inert Phase 0 scalar-preflight slice: the exact
  frozen `SidecarRuntimeConfig`, strict canonical project-root identity,
  versioned project digest, frozen platformdirs lookup, inert P0-001
  `StateStore` root normalization, exact port/timeout admission, package
  exports, and the selected direct runtime dependency pin. It adds no child
  protocol, process, listener, state mutation, SDK lifecycle, source scan,
  storage, OTel, or UI behavior.

Adversarial Reviewer:
- Reviewer 1: design/contract review found missing canonical project-root
  identity, relative platformdirs admission, target-versus-global filesystem
  side-effect wording, all-seam process-control evidence, digest-byte ambiguity,
  and lexical-versus-inode safety boundaries. After the contract fixes, final
  P0/P1/P2 = 0 and GO.
- Reviewer 2: implementation review found fixed-error/context ambiguity,
  incomplete platformdirs and StateStore result admission, absolute collision
  wording, and an ambiguous raw digest separator. After exact messages/order,
  pre-StateStore fail-closed checks, exact result/dictionary fields, and an
  independently fixed digest fixture were required, final P0/P1/P2 = 0 and GO.
- Reviewer 3: production adversary found direct `__new__` construction and an
  exact-dictionary hostile-key collision that could invoke caller-controlled
  `__eq__`. The final implementation blocks direct construction in `__new__`
  and rejects every inexact dictionary key before lookup; the malicious-key
  probe observes zero protocol calls. Case-only, hard-link, and bind-mount
  equivalence remain explicitly outside this lexical identity contract. Final
  P0/P1/P2 = 0 and GO.
- Reviewer 4: test adversary found a keyword-`self` constructor hole, weak
  dependency-name detection, missing independent temporary-root digests,
  missing parent-alias/runtime-root and inexact-fsencode evidence, and several
  static-evidence escape routes. The final suite closes each gap, locks the
  reviewed production choreography, and rejects alternate helpers, callbacks,
  mutable module state, dynamic dispatch, and out-of-scope calls. Final
  P0/P1/P2 = 0 and GO.

Fixer:
- Codex primary accepted every contract and implementation finding. The final
  boundary permits platformdirs/stdlib availability probes but no FlowSight
  target mutation, propagates process-control at every captured dependency and
  operation seam, blocks serialization and alternate construction, and defines
  one exact lexical identity without claiming inode continuity. None were
  deferred.

Quality Governor:
- Independent scope review confirmed one inert Phase 0 scalar-preflight slice
  with the MVP-selected platformdirs dependency. It stops before SDK lifecycle,
  child handoff, wire format, process, listener, election, source scanning,
  storage, and UI behavior. Final implementation review found changes only in
  the four product/test allowlist files plus this task card. P0/P1/P2 = 0 and
  GO.

## Verifier Evidence

- Command: focused runtime-config tests; `make test-phase0`; `make check`;
  `make gate-phase0`
- Result: passed locally; candidate CI pending
- Notes: focused tests passed 289/289; Phase 0 tests passed 1,868/1,868;
  formatting, lint, strict production typing, agent-system checks, and the full
  repository suite passed 1,993/1,993 on local CPython 3.13.5; the sustained
  Phase 0 gate passed. The full check correctly retains its partial-scaffold
  limitation. This evidence proves exact scalar configuration only; it does not
  prove child launch/READY, descriptor handoff, state publication,
  requested-port compatibility, reload, SDK attachment, or complete Phase 0.

## Failure Queue Items

- none
