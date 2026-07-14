# Task: Execute One Exact Sidecar Child Entry

## Task Metadata

```yaml
task_id: P0-023
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-016, P0-022, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-023

## Phase

Phase 0

## Goal

Provide one private executable child-module boundary that reads only the
process-provided argument vector, converts its suffix to one exact built-in
tuple, and calls P0-022's `run_sidecar_child` exactly once without parsing,
altering, logging, or otherwise interpreting any bootstrap field.

## Context

- P0-016 owns the versioned inert child argument schema and P0-022 owns the
  complete adopted child transaction. Neither task creates an executable module
  that a future parent launch transaction can invoke.
- The fixed private execution path is:

  ```text
  python -I -m flowsight.sidecar.child_entry <P0-016 tuple>
      -> tuple(sys.argv[1:])
      -> run_sidecar_child(arguments)
  ```

- This entry is not a public FlowSight or `flowsight.sidecar` API and adds no
  command-line option, schema, parser, environment fallback, exit-code mapper,
  launcher, signal policy, state/store/listener/channel ownership, or runtime
  cleanup. P0-022 alone owns every child resource and startup outcome.
- Invalid arguments reach P0-022 unchanged, so its fixed errors and
  process-control behavior remain the only outcome policy. The entry must not
  catch, wrap, print, log, translate, suppress, or inspect ordinary or
  `BaseException` outcomes.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 2.5, 4.2, 7.4, and Phase 0
  - `docs/agent-operating-system.md` Phase 0 gate and task boundary rules
  - `docs/agent-facts.tsv` FS-001, FS-007, FS-008, and FS-023
  - `spikes/sidecar_otel/RESULT.md` promotion requirements
  - P0-016 and P0-022 contracts

## Related Fact IDs

- FS-001
- FS-007
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/child_entry.py`
- `tests/sidecar/test_child_entry.py`
- `tests/sidecar/test_runtime_config.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

`tests/sidecar/test_runtime_config.py` may change only to add
`child_entry` to its exact private-sidecar-submodule expectation. Importing a
new private child module necessarily registers that submodule on its parent
package; this test-only amendment neither adds nor exports a public API.

## Expected Changed Files

- `flowsight/sidecar/child_entry.py`
- `tests/sidecar/test_child_entry.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not modify P0-016, P0-022, the SDK, public package exports, child runtime,
  parent/launcher orchestration, state/store, listener, startup channel,
  FastAPI/Uvicorn, SQLite/writer, OTel, sender, UI, tracepoint, or build
  configuration.
- Do not add a console-script entrypoint, `flowsight.sidecar.__main__`, public
  callable, option/flag parser, command registry, environment/config fallback,
  exit-code conversion, logging, output, traceback formatting, signal handler,
  thread, task, subprocess, or process management.
- Do not read, split, index, validate, decode, reconstruct, serialize, cache,
  compare, format, print, log, expose, or retain individual argument strings.
  The sole reviewed transformation is the built-in `tuple(sys.argv[1:])`.
- Do not catch `Exception` or `BaseException` around P0-022. In particular do
  not turn a fixed transaction error into `SystemExit`, and do not convert
  `KeyboardInterrupt`, `SystemExit`, or custom direct `BaseException` values.
- Do not claim parent startup, readiness, attach, reload, clean-wheel product
  acceptance, SQLite ownership, or Phase 1 telemetry behavior. This is only a
  narrow execution target for a later parent transaction.

## Acceptance Criteria

- [x] `flowsight.sidecar.child_entry` is importable as a private module but
  introduces no `flowsight.sidecar` export, class, public callable, parser,
  constant, option, or result surface.
- [x] Executing the module calls the import-time-captured P0-022
  `run_sidecar_child` exactly once with `tuple(sys.argv[1:])`; it does not read
  any environment value or inspect an individual suffix element.
- [x] The entry does not catch or alter ordinary errors or process-control
  identity, payload, notes, context, traceback, or exit behavior from P0-022.
- [x] Empty, malformed, and valid P0-016 tuple suffixes are forwarded unchanged;
  no field parsing, defaults, coercion, copy, output, or retained side state is
  introduced.
- [x] Isolated no-shell subprocess evidence executes the installed source path
  with inherited descriptors only where P0-022 already requires them, proving
  the module neither adds a second listener/channel nor leaks token, path,
  descriptor, or raw exception output.
- [x] A positive AST allowlist freezes the exact imports, one tuple operation,
  one captured call, no handlers or nested functions, and the absence of
  argv-field parsing, environment, subprocess, launcher, signal, storage,
  OTel, UI, tracepoint, logging, print, or mutable module state.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass locally and on macOS/Linux x CPython 3.12/3.13 CI. The
  partial-scaffold disclaimer remains explicit.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_child_entry.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
one private executable module forwards one exact argv suffix to P0-022 without
creating any new child, parent, resource, telemetry, or public-API behavior
```

## Risks

- Reading or formatting an individual suffix before P0-022 would duplicate the
  P0-016 decoder boundary and could leak bootstrap descriptors or paths.
- An error wrapper or exit-code mapper would hide P0-022's fixed ordinary error
  or corrupt process-control semantics needed by the future parent transaction.
- Combining this with parent launching would merge two ownership transactions
  and make the P0-019 one-overall-deadline requirement harder to review.

## Reviewer Focus

- Is the only data operation the exact suffix tuple construction followed by
  one captured P0-022 call?
- Can module import or execution expose any argument/token/path/descriptor or
  add any process, signal, parser, state, listener, SQLite, OTel, or UI scope?
- Does every error and control leave P0-022 untouched and preserve its native
  semantics?

## Role Outputs

Implementer:
- Added one private executable module with exactly one suffix-tuple operation
  and one import-time-captured P0-022 call. It has no public package export,
  parser, environment read, handler, output, parent launch, or runtime policy.
- Added forwarding, native ordinary/control propagation, isolated execution,
  capture, and AST tests; updated only the exact private-submodule expectation
  required by Python's parent-package import semantics.

Adversarial Reviewer:
- Reviewer 1: rechecked that the entry contains one `tuple(sys.argv[1:])`, one
  captured P0-022 call, no exception handler, and no imported/created resource
  behavior. P0/P1/P2 = 0/0/0.
- Reviewer 2: rechecked isolated malformed execution, ordinary/control identity,
  non-exported package surface, and source AST rejection of parser, environment,
  launcher, signal, storage, OTel, UI, tracepoint, logging, and mutable state.
  P0/P1/P2 = 0/0/0.

Fixer:
- After the first Phase 0 regression exposed the normal Python parent-package
  submodule registration, amended the task allowlist in its own control-plane
  commit and added only `child_entry` to the existing private-submodule audit.
  No production API or scope expanded.

Quality Governor:
- One Phase 0 private execution-boundary task with exactly its entry module,
  focused test, and exact private-submodule audit expectation;
  `scope_override: none`, no new gate claim, and no parent/Phase 1 drift.

## Verifier Evidence

- Command: `.venv/bin/ruff format --check flowsight/sidecar/child_entry.py tests/sidecar/test_child_entry.py tests/sidecar/test_runtime_config.py`
- Result: passed
- Command: `.venv/bin/ruff check flowsight/sidecar/child_entry.py tests/sidecar/test_child_entry.py tests/sidecar/test_runtime_config.py`; `.venv/bin/mypy flowsight/sidecar/child_entry.py`
- Result: passed; no lint or type errors
- Command: `.venv/bin/python -m pytest tests/sidecar/test_child_entry.py tests/sidecar/test_runtime_config.py -q`
- Result: passed; 306 tests
- Command: `make test-phase0`
- Result: passed; 2522 tests
- Command: `make gate-phase0`
- Result: passed; agent-system checks, formatting, lint, type checks, 2647 tests,
  and the `phase0-sustained` gate all passed; the partial-scaffold disclaimer
  remained explicit
- Command: `.venv/bin/python scripts/validate_agent_system.py`; `git diff --check`
- Result: passed; implementation scope is exactly the task allowlist
- Candidate commit: `d030b8b87d4be430d914b0caef6b0d35fefd8e8d`
- CI: [agent-checks run 29182154972](https://github.com/alovwang-sys/FlowSight/actions/runs/29182154972)
  passed on macOS 3.13 (86621732631), Ubuntu 3.12 (86621732639), macOS 3.12
  (86621732648), and Ubuntu 3.13 (86621732699).
- Notes: candidate CI covers every required operating-system and CPython matrix.
  This completion-only update changes no product or test behavior.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record.
