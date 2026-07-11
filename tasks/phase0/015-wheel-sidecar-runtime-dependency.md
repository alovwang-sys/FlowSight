# Task: Ship the Sidecar Server Runtime Dependency

## Task Metadata

```yaml
task_id: P0-015
release: v1
task_type: implementation
status: planned
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-014, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-015

## Phase

Phase 0

## Goal

Promote the TRIAL-004-tested `uvicorn==0.51.0` pin from a development-only
extra to the sole direct Uvicorn runtime requirement, and prove that installing
only a built FlowSight wheel as the explicit target supplies the future sidecar
child's import-time server dependency.

## Context

- The Phase 0 design selects FastAPI plus Uvicorn for the project-scoped Python
  sidecar, but the current wheel metadata places Uvicorn only behind the `dev`
  extra. A normal `pip install flowsight` therefore does not declare the server
  dependency needed by a future child runtime.
- TRIAL-004 and its supported CI matrix exercised `uvicorn==0.51.0`; this task
  promotes that already-tested exact version and does not upgrade it or select
  new server behavior.
- The clean-wheel probe is deliberately narrower than the eventual bundled-UI
  probe. It proves the installed dependency closure and programmatic Uvicorn
  surface only; it does not claim a child entry point, READY lifecycle, UI
  serving, or complete Phase 0 acceptance.
- Source of truth: MVP design sections 2.1, 2.5, 4.1, 4.2, and 11 Phase 0,
  plus TRIAL-004 promotion requirements.

## Related Fact IDs

- FS-001
- FS-008
- FS-031

## Allowed Files

- `pyproject.toml`
- `Makefile`
- `tests/packaging/test_wheel_runtime_dependency.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `pyproject.toml`
- `Makefile`
- `tests/packaging/test_wheel_runtime_dependency.py`

## Forbidden

- Do not add a sidecar child entry point, CLI, launcher, production runtime
  module, Uvicorn server startup, listener bind, READY signal, runtime process
  spawn, inherited-descriptor adoption, owner handoff, state mutation, or
  cleanup. The probe may start only its bounded build, venv, pip, and isolated
  interpreter commands and must terminate their process group on timeout.
- Do not change existing sidecar primitives or public APIs; add SDK, SQLite,
  OTel, ingest, lease, reload, shutdown, UI, browser, or Node behavior; or
  import spike code into the product package.
- Do not upgrade Uvicorn, request its `standard` extra, pin its transitive
  dependencies, add another dependency, or change any dependency other than
  moving the exact existing Uvicorn pin from `dev` to runtime.
- Do not explicitly install Uvicorn or any FlowSight runtime dependency in the
  isolated probe. The built FlowSight wheel must be its sole explicit install
  target; normal dependency resolution must supply the declared closure.
- Do not inherit pip/user Python configuration into build or install commands.
  Scrub `PIP_*`, `PYTHONPATH`, `PYTHONHOME`, and `VIRTUAL_ENV`; disable pip
  configuration by setting `PIP_CONFIG_FILE` to the platform null device;
  disable interactive/version-check behavior; use the credential-free public
  PyPI simple index with bounded pip retries and network timeout; and retain
  captured command output for a failing assertion.
- Do not reuse the workspace environment, workspace imports, user site,
  `PYTHONPATH`, an existing wheel, or an existing virtual environment as
  clean-wheel evidence.
- Do not start Uvicorn or open a socket in the probe. Importing the fixed
  programmatic API is the only server behavior in this task.
- Do not update FS-031 evidence or claim clean-wheel UI serving, singleton
  ownership, child startup, or complete Phase 0 acceptance.

## Acceptance Criteria

- [ ] `[project].dependencies` contains exactly one unmarked
  `uvicorn==0.51.0` requirement, the `dev` extra contains no Uvicorn
  requirement, and every other direct/runtime and development requirement is
  unchanged.
- [ ] A self-contained automated probe copies only the repository's wheel build
  inputs into a temporary source directory, builds exactly one wheel there,
  creates a fresh virtual environment, and names only that wheel as the
  explicit `pip install` target. It checks every external command's exit status,
  uses bounded timeouts, and terminates the command's process group on timeout.
- [ ] The fresh environment passes `pip check` without installing the
  repository's development extra or explicitly naming Uvicorn or another
  runtime dependency. Its install command uses pip isolated/non-interactive
  mode, the explicit credential-free PyPI simple index, bounded retries and
  network timeout, and the controlled environment defined above.
- [ ] Before installation, the fresh environment has neither a discoverable
  import spec nor installed distribution metadata for `flowsight` or `uvicorn`.
  The later isolated probe proves its resolved `sys.prefix` is exactly the
  newly created virtual environment rather than merely a path prefix match.
- [ ] From a temporary working directory, a child interpreter runs with `-I`,
  empty `PYTHONPATH`, and `PYTHONNOUSERSITE=1`. It imports `flowsight.sidecar`
  and `uvicorn` from paths under that environment's exact `sys.prefix`, proves
  neither import resolves inside the workspace, and imports exact public
  `uvicorn.Config` and `uvicorn.Server` classes without starting a server.
- [ ] The isolated interpreter observes installed Uvicorn version `0.51.0` and
  the installed FlowSight distribution metadata contains exactly one unmarked
  `Requires-Dist: uvicorn==0.51.0`, with no Uvicorn extra marker or duplicate.
- [ ] The probe uses temporary directories, leaves no wheel/venv/build artifact
  in the repository, invokes no shell, binds no socket, starts no server, and
  contains no fallback to workspace source or an already-installed Uvicorn.
- [ ] `make test-phase0` includes the focused packaging probe. Focused tests,
  `make test-phase0`, `make check`, and the sustained Phase 0 gate pass on
  CPython 3.12/3.13 and the macOS/Linux CI matrix while retaining the current
  partial-scaffold limitation.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/packaging/test_wheel_runtime_dependency.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
the isolated wheel installs one exact Uvicorn runtime dependency and all
repository checks pass without claiming complete Phase 0 evidence
```

## Risks

- The ordinary development environment already installs the `dev` extra, so a
  workspace import test would be a false green. Provenance must come from the
  newly built wheel and newly created environment.
- A clean install necessarily exercises public PyPI resolution and can be
  slower than unit tests or fail when that service is unavailable. Each command
  and network attempt is bounded, pip may use its normal content cache, and
  captured output remains available on failure. The verification sequence
  intentionally invokes the same self-contained test through focused, Phase 0,
  full-check, and gate commands; every invocation uses fresh temporary paths
  and the same limits rather than a persistent wheel or environment.
- Importability is only a prerequisite for the future child runtime. It says
  nothing about inherited ownership, Uvicorn readiness, publication, or
  lifecycle cleanup, which remain later Phase 0 tasks.

## Reviewer Focus

- Can the test pass because the workspace, user site, dev extra, or an explicit
  Uvicorn install supplied the dependency instead of wheel metadata?
- Does the diff change any dependency besides the exact Uvicorn move or claim
  child/server/UI behavior that it does not exercise?
- Are all subprocesses bounded, checked, and constrained to temporary paths
  without starting a server or opening a listener?

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

- Command: TBD
- Result: TBD
- Notes: TBD

## Failure Queue Items

- TBD
