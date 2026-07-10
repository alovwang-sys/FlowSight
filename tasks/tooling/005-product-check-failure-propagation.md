# Task: Make Product Checks Propagate Failures

## Task Metadata

```yaml
task_id: TOOL-005
release: v1
task_type: tooling
status: in_progress
primary_phase: phase0
impacted_phases: [phase1, phase4]
depends_on: [TOOL-004]
requires_gates: []
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

TOOL-005

## Phase

Phase 0

## Goal

Make every product-check recipe return nonzero when an internal static check,
test, or build command fails, so a trailing informational message cannot let a
gate target continue after failed verification.

## Context

- `make gate-phase0 gate-phase1 gate-phase4` observed one failed pytest test but
  exited successfully because `check-product` continued to its final
  partial-scaffold message.
- The same multi-command shell shape is shared by `check-product-fast` and
  `test-product`.
- Shared enforcement belongs in the tool-neutral Makefile and its existing
  product-detection fixture.

## Related Fact IDs

- FS-023

## Allowed Files

- `Makefile`
- `scripts/agent/test_product_detection.py`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `Makefile`
- `scripts/agent/test_product_detection.py`

## Forbidden

- Do not weaken or skip any Python/frontend check.
- Do not make informational partial-scaffold output count as acceptance.
- Do not change gate opener/fact state or product behavior.
- Do not edit tool-specific adapters.

## Acceptance Criteria

- [ ] `check-product` returns nonzero when its late Python test command fails.
- [ ] `check-product-fast` returns nonzero when a Python static command fails.
- [ ] `test-product` returns nonzero when its Python test command fails.
- [ ] A gate-style dependent target does not execute after its product-check prerequisite fails.
- [ ] Existing Python-only/frontend detection cases and full repository checks pass.

## No-Test Reason

N/A

## Verification

Run:

```sh
python3 scripts/agent/test_product_detection.py
make check
```

Expected result:

```text
fixture failures propagate nonzero and all normal repository checks pass
```

## Risks

- Shell `set -e` behavior can be subtle inside conditionals.
- A fixture that fails too early may not prove the trailing message was the mask.
- Frontend-only and pre-scaffold branches must retain their current behavior.

## Reviewer Focus

- Does the regression fail against the pre-fix Makefile for the right reason?
- Are late Python failures propagated without changing detection semantics?
- Can any gate recipe still run after its check prerequisite fails?

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

- Command: `python3 scripts/agent/test_product_detection.py && make check`
- Result: pending
- Notes: task activated after the masked pytest failure was reproduced

## Failure Queue Items

- none
