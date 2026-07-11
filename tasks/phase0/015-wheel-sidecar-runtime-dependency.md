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
- The repository CI matrix runs `make check`; focused, Phase 0, and sustained
  gate commands are local verifier evidence. Matrix evidence is valid only when
  each `make check` job collects and passes this self-contained probe.
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
  First remove every inherited `PIP_*` and `PYTHON*` variable plus
  `VIRTUAL_ENV`, `HOME`, every `XDG_*` variable, `NETRC`, and `SSH_AUTH_SOCK`.
  Recreate `HOME`, XDG config/cache/data, and pip cache as empty paths inside
  the probe temporary directory; restore only the reviewed empty/user-site and
  pip controls. Disable all pip configuration with `PIP_CONFIG_FILE` set to the
  platform null device, disable keyring, interaction, and version checks, and
  use only the credential-free `https://pypi.org/simple` index with two retries
  and a 15-second per-network-operation timeout. The isolated build's internal
  pip receives the same controls. Retain captured command output for failures.
- Do not reuse the workspace environment, workspace imports, user site,
  `PYTHONPATH`, an existing wheel, or an existing virtual environment as
  clean-wheel evidence.
- Do not copy an existing `dist/`, `build/`, `*.egg-info`, `*.dist-info`, cache,
  symlink, device, socket, or other non-regular input into the temporary build
  source. Do not use a requirements/constraints file, `--find-links`,
  `--extra-index-url`, `--no-deps`, or any second install requirement.
- Do not start Uvicorn or open a socket in the probe. Importing the fixed
  programmatic API is the only server behavior in this task.
- Do not update FS-031 evidence or claim clean-wheel UI serving, singleton
  ownership, child startup, or complete Phase 0 acceptance.

## Acceptance Criteria

- [ ] `[project].dependencies` is exactly `fastapi==0.139.0`,
  `platformdirs==4.10.0`, `pydantic==2.13.4`, `starlette==1.3.1`, and one
  unmarked `uvicorn==0.51.0`, in that order. The `dev` extra is exactly
  `asgiref==3.11.1`, `build==1.5.1`, `mypy==2.2.0`,
  `opentelemetry-api==1.43.0`, `opentelemetry-instrumentation-fastapi==0.64b0`,
  `opentelemetry-sdk==1.43.0`, `pytest==9.1.1`, `ruff==0.15.21`, and
  `wrapt==2.2.2`, in that order. Tests hard-code both complete lists so no other
  dependency can move or change unnoticed.
- [ ] A self-contained automated probe copies only the repository's wheel build
  inputs into a temporary source directory whose top level is exactly
  `pyproject.toml` plus `flowsight/`. Eligible package inputs are regular,
  non-symlink Python files, `flowsight/py.typed`, and any existing regular
  `flowsight/static/index.html` or `flowsight/static/assets/**` package data;
  caches and generated metadata are rejected. Resolved source, initially empty
  wheel outdir, virtual environment, and probe cwd are distinct paths outside
  the resolved workspace. The isolated build uses that temporary source as its
  only source and produces exactly one regular, non-symlink
  `flowsight-*.whl` in the outdir.
- [ ] Every external command goes through one no-shell `Popen` runner with
  `start_new_session=True`, closed stdin, combined captured output, exact exit
  checking, and fixed total limits: build 180 seconds, venv 60 seconds, install
  240 seconds, and preflight/pip-check/postflight 30 seconds each. Timeout sends
  `SIGTERM` to the whole process group, waits at most one second, escalates to
  `SIGKILL`, and reaps the leader within five seconds. A deterministic real
  leader-plus-descendant regression proves both processes stop, escalation is
  exercised, and pre-timeout output is present in the failure. Process-control
  `BaseException` cleanup preserves the original exception identity.
- [ ] The fresh environment passes `pip check` without installing the
  repository's development extra or explicitly naming Uvicorn or another
  runtime dependency. Its install command uses pip isolated/non-interactive
  mode, the explicit credential-free PyPI simple index, bounded retries and
  network timeout, disabled keyring, and the controlled environment defined
  above. Its only requirement token is the resolved newly built wheel.
- [ ] The same controlled environment is passed to build, venv, pip, and both
  isolated probes. After caller variables are removed it sets only reviewed
  `PIP_CONFIG_FILE`, `PIP_NO_INPUT`, `PIP_DISABLE_PIP_VERSION_CHECK`,
  `PIP_INDEX_URL`, `PIP_RETRIES`, `PIP_DEFAULT_TIMEOUT`,
  `PIP_KEYRING_PROVIDER`, `PIP_CACHE_DIR`, empty `PYTHONPATH`,
  `PYTHONNOUSERSITE=1`, and temporary `HOME`/XDG paths in those namespaces.
  Tests inject hostile inherited configuration and credential paths and prove
  none survive.
- [ ] Before installation, the fresh environment has neither a discoverable
  import spec nor installed distribution metadata for `flowsight` or `uvicorn`.
  The later isolated probe proves its resolved `sys.prefix` is exactly the
  newly created virtual environment rather than merely a path prefix match.
- [ ] From a temporary working directory, a child interpreter runs with `-I`,
  empty `PYTHONPATH`, and `PYTHONNOUSERSITE=1`. It imports `flowsight.sidecar`
  and `uvicorn` from paths under that environment's exact `sys.prefix`, proves
  neither import resolves inside the workspace, temporary source, or wheel
  outdir, and imports exact public `uvicorn.Config` and `uvicorn.Server` class
  identities without instantiating either. An audit hook fails on any socket or
  subprocess event during these imports.
- [ ] The isolated interpreter observes installed Uvicorn version `0.51.0` and
  the installed FlowSight distribution metadata contains exactly one unmarked
  `Requires-Dist: uvicorn==0.51.0`, with no Uvicorn extra marker or duplicate.
- [ ] Postflight proves the lexical `sys.executable` is the expected venv
  launcher and resolved `sys.prefix` equals the new venv exactly (the launcher
  itself may resolve through a base-interpreter symlink on macOS). Resolved
  `flowsight.__file__`, `flowsight.sidecar.__file__`, `uvicorn.__file__`, and
  both distributions' `METADATA`/`.dist-info` paths are contained by that exact
  prefix. Workspace, temporary source, and outdir are absent from `sys.path`
  and from every verified origin. All containment uses `Path.resolve()` and
  `Path.is_relative_to()`, never string-prefix matching.
- [ ] The probe uses temporary directories, leaves no wheel/venv/build artifact
  in the repository, invokes no shell, binds no socket, starts no server, and
  contains no fallback to workspace source or an already-installed Uvicorn.
- [ ] `make test-phase0` includes the focused packaging probe. Focused tests,
  `make test-phase0`, `make check`, and the sustained Phase 0 gate pass locally.
  Every macOS/Linux × CPython 3.12/3.13 CI `make check` job collects and passes
  the probe while retaining the current partial-scaffold limitation.

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
  and network attempt is bounded, build and install share only a probe-local
  temporary pip cache, and captured output remains available on failure. The
  verification sequence intentionally invokes the same self-contained test
  through focused, Phase 0, full-check, and gate commands; every invocation uses
  fresh temporary paths and the same limits rather than a persistent wheel or
  environment.
- Importability is only a prerequisite for the future child runtime. It says
  nothing about inherited ownership, Uvicorn readiness, publication, or
  lifecycle cleanup, which remain later Phase 0 tasks.

## Reviewer Focus

- Can the test pass because the workspace, user site, dev extra, or an explicit
  Uvicorn install supplied the dependency instead of wheel metadata?
- Can a caller's pip/Python config, HOME/XDG credential source, keyring, netrc,
  existing wheel, requirements file, or alternate index influence the result?
- Does the diff change any dependency besides the exact Uvicorn move or claim
  child/server/UI behavior that it does not exercise?
- Are all subprocesses bounded, checked, and constrained to temporary paths
  without starting a server or opening a listener, and does the timeout test
  prove descendant cleanup rather than only inspecting runner source?

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
