# Task: Wire SDK Initialization to the Project Sidecar

## Task Metadata

```yaml
task_id: P0-027
release: v1
task_type: implementation
status: review
primary_phase: phase0
impacted_phases: []
depends_on: [P0-024, P0-026, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-027

## Phase

Phase 0

## Goal

Make `FlowSight.init_app(app)` prepare the exact project runtime configuration
and start or attach the one project-scoped sidecar while preserving idempotent,
retryable SDK initialization.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 2.2 First-version user loop
  - 4.2 Runtime Process Model
  - 10.1 SDK API
  - 11 Phase 0

## Related Fact IDs

- FS-007
- FS-008
- FS-024
- FS-030

## Allowed Files

- `flowsight/sdk/lifecycle.py`
- `tests/test_sdk_skeleton.py`

The current task card and its verifier evidence are always writable control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sdk/lifecycle.py`
- `tests/test_sdk_skeleton.py`

## Forbidden

- Do not change sidecar startup primitives or their public contracts.
- Do not add OTel instrumentation, producer leases, event ingest, storage writes, or Phase 1 behavior.
- Do not add UI opening, token logging, shutdown semantics, or frontend/packaging behavior.
- Do not expose the admitted sidecar token or state through the public SDK API.
- Do not weaken or delete existing sidecar process tests.

## Acceptance Criteria

- [x] Constructing `FlowSight` remains side-effect free.
- [x] `init_app()` prepares runtime configuration from the configured project root and UI port, then uses the existing bounded start-or-attach transaction.
- [x] Sequential and concurrent duplicate initialization of the same SDK/app pair performs one startup transaction and returns `None` each time.
- [x] A failed startup does not mark the app initialized, so a later call can retry.
- [x] Initialization does not mutate FastAPI routes, middleware, or app state and does not open a browser or disclose the startup token.
- [x] Targeted SDK tests, Phase 0 tests, the full repository check, and the Phase 0 gate pass.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest -q tests/test_sdk_skeleton.py tests/sidecar/test_parent_runtime.py
make test-phase0
make check
make gate-phase0
git diff --check
```

Expected result:

```text
SDK initialization starts or attaches exactly one project sidecar and all Phase 0 checks pass
```

## Risks

- Marking an app initialized before startup succeeds could make a transient failure permanent.
- Unsynchronized duplicate calls could dispatch multiple parent startup transactions.
- Retaining or exposing the admitted state could leak the capability token.

## Reviewer Focus

- Is the initialization commit point after successful sidecar admission?
- Are duplicate calls serialized without changing sidecar ownership semantics?
- Does the slice remain Phase 0 lifecycle wiring with no Phase 1 or UI behavior?

## Role Outputs

Implementer:
- Wired the public SDK lifecycle to the reviewed runtime-config and
  start-or-attach primitives, committing app initialization only after exact
  sidecar admission and serializing duplicate calls.

Adversarial Reviewer:
- Reviewer 1: A dedicated adversarial pass checked the failure commit point,
  concurrent lock boundary, exact-state admission, public surface, FastAPI
  mutation, and token/error rendering; no behavioral finding remained.
- Reviewer 2: waived: the narrow two-file lifecycle slice has one dedicated
  adversarial pass plus full Phase 0 and repository coverage

Fixer:
- Applied the only mechanical finding by sorting imports; no behavioral fix or
  scope expansion was required.

Quality Governor:
- The diff is limited to the active card plus its two allowlisted files, stays
  in Phase 0, and adds no OTel, ingest, storage, UI, shutdown, dependency, or
  sidecar-primitive behavior.

## Verifier Evidence

- Command: `.venv/bin/python -m pytest -q tests/test_sdk_skeleton.py tests/sidecar/test_parent_runtime.py`; `make test-phase0`; `make check`; `make gate-phase0`; `git diff --check`
- Result: passed
- Notes: Targeted coverage passed 78 tests; Phase 0 passed 2,644 tests; full
  check and the Phase 0 gate each passed 2,774 tests on CPython 3.13.5. The
  full checks correctly retained the partial-scaffold warning rather than
  claiming Phase 0 product acceptance.

## Failure Queue Items

- none
