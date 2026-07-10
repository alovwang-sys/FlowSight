# Task: Isolate Validator Gate-State Test Fixtures

## Task Metadata

```yaml
task_id: TOOL-004
release: v1
task_type: tooling
status: in_progress
primary_phase: phase0
impacted_phases: [phase1, phase4]
depends_on: [TOOL-003]
requires_gates: []
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

TOOL-004

## Phase

Phase 0

## Goal

Make validator unit tests independent of the repository's live gate state so a
real gate can open without invalidating a test that assumes it remains closed.

## Context

- `test_planned_tasks_keep_phase_gate_closed` currently loads the real task
  graph and asserts that `phase0-sustained` is closed.
- TRIAL-004 must eventually complete and promote the remaining Phase 0 facts,
  at which point that assertion becomes false even though validator behavior is
  correct.
- Unit tests must prove closed/open gate behavior with isolated synthetic cards
  and facts; the CLI gate target remains responsible for evaluating live state.

## Related Fact IDs

- FS-023

## Allowed Files

- `scripts/agent/test_validate_agent_system.py`
- `tasks/tooling/004-validator-gate-fixture-isolation.md`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `scripts/agent/test_validate_agent_system.py`

## Forbidden

- Do not change validator production behavior, canonical gate openers, or gate facts.
- Do not force any real gate open or rewrite trial/fact evidence.
- Do not weaken closed-gate, no-go, planned-evidence, or downstream-task checks.
- Do not modify product, spike, frontend, or workflow files.

## Acceptance Criteria

- [ ] No validator unit test requires the live `phase0-sustained` gate to remain closed.
- [ ] A synthetic planned opener proves an incomplete task keeps its isolated gate closed.
- [ ] A synthetic completed opener with command-backed existing-test evidence proves the same isolated gate can open.
- [ ] Every temporary global/root override is restored even if an assertion fails.
- [ ] The validator suite and full `make check` pass without changing production validator code.

## No-Test Reason

N/A

## Verification

Run:

```sh
python3 scripts/agent/test_validate_agent_system.py
make check
```

Expected result:

```text
validator tests remain green whether the repository's real gates are open or closed
```

## Risks

- A synthetic fixture may accidentally mutate class/module globals for later tests.
- Replacing the live-state assertion could silently remove closed-gate coverage.
- A broad refactor could change validator behavior instead of only its tests.

## Reviewer Focus

- Are both closed and open outcomes still exercised with concrete evidence?
- Are `GATE_OPENERS`, `GATE_FACTS`, and `ROOT` restored in `finally` blocks?
- Is the production validator byte-for-byte unchanged?

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

- Command: `python3 scripts/agent/test_validate_agent_system.py && make check`
- Result: TBD
- Notes: task boundary activation only; implementation not yet started

## Failure Queue Items

- none
