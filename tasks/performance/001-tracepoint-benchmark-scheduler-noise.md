# Task: Harden Tracepoint Benchmark Against Scheduler Noise

## Task Metadata

```yaml
task_id: PERF-001
release: v1
task_type: spike
status: planned
primary_phase: phase4
impacted_phases: []
depends_on: [TRIAL-005]
requires_gates: [phase4-tracepoint]
opens_gates: []
spike_decision: pending
scope_reductions: pending
scope_override: none
scope_override_approved_by: none
```

## Task ID

PERF-001

## Phase

Phase 4

## Goal

Replace scheduler-sensitive wall-clock performance decisions in the frozen
tracepoint harness with a dual-clock contract whose unchanged six budgets use
thread CPU time while wall time remains diagnostic.

## Context

- Three fresh-process serial runs on a 12-logical-CPU host at load average
  26--36 failed the absolute wall-clock unscoped/hit budgets while both paired
  no-hit ratios passed every time.
- `perf_counter_ns` includes time when the benchmark thread is descheduled, so
  the current four absolute checks can fail without additional FlowSight CPU
  work.
- TRIAL-005's isolated Ubuntu/macOS x CPython 3.12/3.13 matrix passed the same
  unchanged thresholds; this task corrects the measurement contract, not the
  supported tracepoint subset or Phase 5 request SLA.

## Related Fact IDs

- FS-015
- FS-016
- FS-017
- FS-020
- FS-033

## Allowed Files

- `spikes/tracepoint_backend/benchmark.py`
- `spikes/tracepoint_backend/RESULT.md`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `spikes/tracepoint_backend/benchmark.py`
- `spikes/tracepoint_backend/RESULT.md`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`

## Forbidden

- Do not raise, remove, retry-until-green, or conditionally skip any of the six
  accepted performance thresholds.
- Do not change tracepoint runtime behavior, safe-summary behavior, supported
  function/context shapes, or Phase 5's 10 ms request SLA.
- Do not treat thread CPU time as an end-to-end latency measurement.
- Do not edit CI/workflow wiring unless a reviewed task-boundary amendment
  explicitly adds it.

## Acceptance Criteria

- [ ] Every timed pair records thread CPU time and diagnostic monotonic wall
  time; all six unchanged thresholds are evaluated from thread CPU metrics.
- [ ] Deterministic fake-clock tests prove scheduler wait changes wall
  diagnostics without changing the CPU-budget decision.
- [ ] A failed budget assertion names each failed check with observed and maximum
  values instead of exposing only an aggregate boolean.
- [ ] The new schema/digest and supersession rationale are recorded consistently
  in the benchmark, result, and MVP design.
- [ ] CPython 3.12/3.13 focused tests, `make check`, and isolated Ubuntu/macOS x
  CPython 3.12/3.13 CI pass without changing tracepoint support claims.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/spikes/test_tracepoint_backend.py
make check
```

Expected result:

```text
all six unchanged CPU-time budgets and all repository checks pass
```

## Risks

- A partial clock conversion could compare CPU observations with wall-derived
  fields or silently change workload identity.
- Process CPU time would include unrelated threads; the contract must use the
  current benchmark thread's CPU clock.
- Digest/evidence drift could leave accepted results referring to the old
  scheduler-sensitive harness.

## Reviewer Focus

- Are all budget inputs thread CPU metrics while wall fields are diagnostic only?
- Do fake-clock tests prove the decision boundary without relying on sleeps?
- Are the six numeric maxima byte-for-byte unchanged and digest-covered?
- Does any evidence wording accidentally broaden Phase 4 product support?

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

- Command: pending
- Result: pending
- Notes: task created from the TOOL-005 full-check failure and three controlled
  fresh-process reproductions

## Failure Queue Items

- none
