# Task: Create Minimal SDK Skeleton

## Task Metadata

```yaml
task_id: TRIAL-001
release: v1
task_type: implementation
status: in_progress
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

- [ ] `from flowsight import FlowSight` works.
- [ ] `FlowSight().init_app(app)` is callable with a FastAPI app.
- [ ] The API shape matches the MVP example.
- [ ] Tests prove no collection side effects are required for construction.
- [ ] `make check` discovers and runs the product test suite; its output distinguishes bootstrap checks from product evidence.

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
- TBD

Adversarial Reviewer:
- Reviewer 1: TBD
- Reviewer 2: TBD

Fixer:
- TBD

Quality Governor:
- TBD

## Verifier Evidence

- Command: `make check`
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
