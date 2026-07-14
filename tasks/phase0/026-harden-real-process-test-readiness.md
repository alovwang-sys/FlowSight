# Task: Harden Real-Process Test Readiness Budgets

## Task Metadata

```yaml
task_id: P0-026
release: v1
task_type: tooling
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-025, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-026

## Phase

Phase 0

## Goal

Keep real-process Phase 0 and TRIAL-004 tests from treating isolated-interpreter
import time as product startup behavior while preserving every product timeout
and negative handoff assertion.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 4.2 Runtime Process Model
  - 6.2 OpenTelemetry ownership contract
- A merge-review `make check` ran with 12 CPUs at a 24--28 percent CPU speed
  limit and load average above 35. Isolated `flowsight.sidecar` import took 3.53
  seconds, exceeding fixed 2--5 second test-harness startup budgets. The same
  final SHA passed eight Ubuntu/macOS x CPython 3.12/3.13 CI jobs, and no
  FlowSight process leak remained after the local failures.

## Related Fact IDs

- FS-008
- FS-010
- FS-024

## Allowed Files

- `tests/sidecar/test_child_runtime.py`
- `tests/sidecar/test_parent_handoff.py`
- `tests/spikes/test_sidecar_otel_lifecycle.py`
- `queue/failures.jsonl`

The current task card and its verifier evidence are always writable control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `tests/sidecar/test_child_runtime.py`
- `tests/sidecar/test_parent_handoff.py`
- `tests/spikes/test_sidecar_otel_lifecycle.py`
- `queue/failures.jsonl`

## Forbidden

- Do not change production or spike runtime timeout defaults or behavior.
- Do not change the 0.2 second pre-handoff negative assertion.
- Do not relax lease, shutdown, request, health, or startup-channel semantics.
- Do not add Phase 1 telemetry, public API, frontend, or product behavior.
- Do not skip, delete, or retry-until-green any test.

## Acceptance Criteria

- [x] Real isolated-child positive readiness and exit observations use one
      bounded test-harness budget large enough for a cold isolated import.
- [x] The pre-release handoff assertion remains 0.2 seconds and still proves no
      publication before parent EOF.
- [x] Non-startup TRIAL-004 lifecycle cases use a bounded process-start budget
      without changing the behavior-specific lease/shutdown/request deadlines.
- [x] Focused P0-025 and TRIAL-004 regressions pass with no leaked child,
      Uvicorn, pytest, or sidecar process.
- [x] `make test-phase0`, `make check`, and `make gate-phase0` pass in supported
      verification environments.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest -q \
  tests/sidecar/test_child_runtime.py \
  tests/sidecar/test_parent_handoff.py \
  tests/spikes/test_sidecar_otel_lifecycle.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
real-process tests pass without changing product timeout semantics
```

## Risks

- Broadly increasing behavior-specific deadlines could hide a product hang.
- Updating only one cold-start observation could leave sibling tests flaky.
- Test cleanup must remain bounded even when the child ignores SIGTERM.

## Reviewer Focus

- Are only positive cold-start/readiness observations widened?
- Do all negative and product behavior deadlines retain their exact semantics?
- Is every change confined to test harnesses and failure evidence?

## Role Outputs

Implementer:
- Added test-only bounded constants for cold isolated-child observations and
  the four affected non-startup TRIAL-004 lifecycle setups. Production and
  spike runtime code is unchanged.

Adversarial Reviewer:
- Reviewer 1: Found one P1 where the initial patch also widened post-import
  handoff and shutdown observations. After the original 2/10 second bounds
  were restored, re-review found no remaining P0/P1/P2.
- Reviewer 2: waived: one independent integration reviewer plus focused
  Phase 0 diagnostic reviewer will cover this test-only repair.

Fixer:
- Applied the two diagnosed failure signatures without changing negative
  assertions, product timeouts, thresholds, or runtime behavior.
- Accepted the independent P1 finding and restored the original EOF-to-ready
  and post-Ready exit limits; only observations that still include a cold
  isolated import use the wider harness budget.

Quality Governor:
- The candidate changes only P0-026 allowlisted tests and failure records; no
  Phase 1 product code, API, UI, dependency, or runtime behavior changed.

## Verifier Evidence

- Command: six original failing nodes; full three-module focused suite; Ruff
  format/lint; agent-system validator; process-leak inspection.
- Result: passed
- Notes: The six original nodes passed in 6.11 seconds; after the review fix,
  the full 134-test focused suite passed in 42.13 seconds. `make test-phase0`
  passed 2,641 tests, `make check` passed 2,766 tests, and `make gate-phase0`
  passed the same suite plus `phase0-sustained`. Push run `29296141027` and PR
  run `29296143012` both passed Ubuntu/macOS x CPython 3.12/3.13. No pytest,
  Uvicorn, spike sidecar, or production sidecar process remained.

## Failure Queue Items

- FSQ-0002
- FSQ-0003
