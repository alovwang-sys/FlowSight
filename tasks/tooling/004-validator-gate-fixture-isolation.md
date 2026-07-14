# Task: Isolate Validator Gate-State Test Fixtures

## Task Metadata

```yaml
task_id: TOOL-004
release: v1
task_type: tooling
status: complete
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
- `test_cli_reports_closed_gate` likewise assumes the live
  `phase4-tracepoint` gate can never open.
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

- [x] No validator unit test requires a live Phase 0 or Phase 4 gate to remain closed.
- [x] A synthetic planned opener proves an incomplete task keeps its isolated gate closed.
- [x] A synthetic completed opener with command-backed existing-test evidence proves the same isolated gate can open.
- [x] Every temporary global/root override is restored even if an assertion fails.
- [x] The validator suite and full `make check` pass without changing production validator code.

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
- A CLI smoke test that accepts both outcomes could miss output/exit-code drift
  unless it first computes the authoritative live blockers.
- A broad refactor could change validator behavior instead of only its tests.

## Reviewer Focus

- Are both closed and open outcomes still exercised with concrete evidence?
- Are `GATE_OPENERS`, `GATE_FACTS`, and `ROOT` restored in `finally` blocks?
- Is the production validator byte-for-byte unchanged?

## Role Outputs

Implementer:
- Replaced the live Phase 0 closed-state assumption with a synthetic planned
  opener and made the Phase 4 CLI smoke compare its subprocess result with the
  authoritative current blockers, without changing production validator code.

Adversarial Reviewer:
- Reviewer 1: no P0/P1/P2 findings; synthetic planned-opener isolation
  preserves closed/open, no-go, fact-evidence, and downstream-gate coverage,
  restores all temporary globals under forced failures, and the state-agnostic
  CLI wiring smoke matches authoritative live blockers.
- Reviewer 2: waived: one test-only fixture-isolation correction with an
  independent Quality Governor, full validator coverage, and no production
  validator diff

Fixer:
- Accepted the review watch on the identical Phase 4 live-closed assumption and
  extended the correction to the CLI smoke; no other findings required changes.

Quality Governor:
- No process drift: only the allowlisted test and task card changed, the
  production validator matches HEAD byte-for-byte, synthetic closed/open
  coverage remains deterministic, the CLI smoke checks live blockers without
  assuming either gate stays closed, and all overrides remain finally-restored.

## Verifier Evidence

- Command: `python3 scripts/agent/test_validate_agent_system.py && make check`
- Result: passed
- Notes: Validator tests passed 11/11; adversarial focused gate tests passed
  6/6 and forced-failure probes restored `GATE_OPENERS`, `GATE_FACTS`, and
  `ROOT`; `make check` passed all agent/static checks and 186 Python tests in
  39.37s. `scripts/validate_agent_system.py` remained byte-for-byte identical
  to HEAD at blob `9533e99d2fab036b4f82e71ff59f1fb40acbc77f`.

## Failure Queue Items

- none
