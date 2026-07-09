# Task: Create Minimal SDK Skeleton

## Task Metadata

```yaml
task_id: TRIAL-001
release: v1
task_type: implementation
status: review
primary_phase: phase0
impacted_phases: []
depends_on: []
requires_gates: []
opens_gates: [phase0-sustained]
scope_override: none
scope_override_approved_by: none
```

## Task ID

TRIAL-001

## Phase

Phase 0

## Goal

Create the smallest Python package shape that exposes `FlowSight` and makes `FlowSight().init_app(app)` callable without implementing runtime collection.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 2.2 first user loop
  - 4.2 runtime process model
  - 4.4 repository module boundaries
  - 11 Phase 0

## Related Fact IDs

- FS-001
- FS-002
- FS-007
- FS-008

## Allowed Files

- `flowsight/__init__.py`
- `flowsight/py.typed`
- `flowsight/sdk/**`
- `examples/**`
- `tests/**`
- `pyproject.toml`
- `Makefile`
- `.gitignore`

## Expected Changed Files

- `flowsight/__init__.py`
- `flowsight/py.typed`
- `flowsight/sdk/__init__.py`
- `flowsight/sdk/lifecycle.py`
- `examples/fastapi_demo.py`
- `tests/test_sdk_skeleton.py`
- `pyproject.toml`
- `Makefile`
- `.gitignore`

## Forbidden

- Do not add OTel ingest.
- Do not add tracepoints.
- Do not add UI behavior beyond a placeholder if needed.
- Do not bind anything other than `127.0.0.1`.
- Do not implement data lineage.

## Acceptance Criteria

- [x] `from flowsight import FlowSight` works.
- [x] `FlowSight().init_app(app)` is callable with a FastAPI app.
- [x] The API shape matches the MVP example.
- [x] Tests prove no collection side effects are required for construction.
- [x] `make check` discovers and runs the product test suite; its output distinguishes bootstrap checks from product evidence.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/test_sdk_skeleton.py
make check
```

Expected result:

```text
targeted SDK test and shared checks passed
```

## Risks

- Implementer may accidentally start Phase 1 collector work.
- Public API choices made here will shape later tasks.

## Reviewer Focus

- Did this stay Phase 0 only?
- Does it preserve local-only assumptions?
- Is the API consistent with the MVP document?

## Role Outputs

Implementer:
- Added the minimal typed `flowsight` package, FastAPI-only lifecycle API, runnable demo, exact supported dependency pins, product lint/type/test configuration, and clean packaging metadata without runtime collection behavior.

Adversarial Reviewer:
- Reviewer 1: Found unbounded Starlette/Pydantic resolution, constructor-only side-effect coverage, and an overly exact public export assertion; all accepted findings were fixed and rechecked.
- Reviewer 2: Verified the Git-index-only slice, package/module direction, FastAPI boundary, wheel shape, and partial-scaffold disclosure; confirmed untracked UI files are outside this task and left cross-instance/concurrent idempotence to TRIAL-004.

Fixer:
- Pinned FastAPI/Pydantic/Starlette together, extended forbidden-runtime gateway tests across construction and `init_app`, relaxed the future-facing export assertion, narrowed task paths, and kept generated egg metadata out of Git.

Quality Governor:
- Approved the 10-path Phase 0 slice: all staged files match the narrowed boundary, reviewer findings are closed, the wheel contains only Python modules plus `py.typed`, and checks explicitly label the result as partial-scaffold evidence rather than an open Phase 0 gate.

## Verifier Evidence

- Command: `git checkout-index` exact snapshot followed by `make check`, isolated wheel build, and clean-venv `python -I` import plus repeated `FlowSight().init_app(FastAPI())`
- Result: PASS
- Notes: Independent verifier checked index tree `d3fcd0e6671023fbfaa17b9b9e651df9a6b90cc7` with 10 staged records. Guard 107/43, formatter 4, allowlist 23, validator 11, Ruff, mypy, and SDK pytest 4/4 passed; the 8-file wheel contained no UI/static files and clean CPython 3.13.5 imported FlowSight 0.1.0a0/FastAPI 0.139.0 from site-packages. Local output explicitly said partial scaffold, not Phase 0 evidence. Python 3.12 remains a required CI-matrix check before the task closure commit.

## Failure Queue Items

- none
