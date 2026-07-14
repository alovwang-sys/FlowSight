# Task: Wire SDK Initialization to the Project Sidecar

## Task Metadata

```yaml
task_id: P0-027
release: v1
task_type: implementation
status: planned
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

- [ ] Constructing `FlowSight` remains side-effect free.
- [ ] `init_app()` prepares runtime configuration from the configured project root and UI port, then uses the existing bounded start-or-attach transaction.
- [ ] Sequential and concurrent duplicate initialization of the same SDK/app pair performs one startup transaction and returns `None` each time.
- [ ] A failed startup does not mark the app initialized, so a later call can retry.
- [ ] Initialization does not mutate FastAPI routes, middleware, or app state and does not open a browser or disclose the startup token.
- [ ] Targeted SDK tests, Phase 0 tests, the full repository check, and the Phase 0 gate pass.

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
- pending

Adversarial Reviewer:
- Reviewer 1: pending
- Reviewer 2: waived: this narrow two-file lifecycle slice receives one independent adversarial diff review

Fixer:
- pending

Quality Governor:
- pending

## Verifier Evidence

- Command: pending
- Result: pending
- Notes: pending

## Failure Queue Items

- none
