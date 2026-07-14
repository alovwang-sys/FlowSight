# Task: Reconcile Tracepoint Performance Evidence

## Task Metadata

```yaml
task_id: PERF-002
release: v1
task_type: docs
status: in_progress
primary_phase: phase4
impacted_phases: []
depends_on: [PERF-001]
requires_gates: [phase4-tracepoint]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

PERF-002

## Phase

Phase 4

## Goal

Make the immutable schema-v3 matrix evidence and failure-queue status agree
without changing the tracepoint benchmark, thresholds, schema, digest, tests,
or runtime behavior.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 6.5 Tracepoint backend risk gate
- PERF-001's result still describes its matrix as pending in one historical
  paragraph even though the final section records successful run
  `29135246585`.
- FSQ-0001 records one later `active_no_hit` ratio failure on an unchanged
  docs-only commit. The unchanged digest subsequently passed the final branch
  push and PR matrices. A separate local absolute-budget failure occurred only
  while the 12-CPU host was limited to 24--28 percent CPU speed and the full
  suite took 683 seconds; after the host returned to 100 percent speed, the
  same unchanged benchmark passed `make check` twice.

## Related Fact IDs

- FS-015
- FS-016
- FS-017
- FS-020
- FS-033

## Allowed Files

- `spikes/tracepoint_backend/RESULT.md`
- `tasks/performance/001-tracepoint-benchmark-scheduler-noise.md`
- `queue/failures.jsonl`

The current task card and its verifier evidence are always writable control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `spikes/tracepoint_backend/RESULT.md`
- `tasks/performance/001-tracepoint-benchmark-scheduler-noise.md`
- `queue/failures.jsonl`

## Forbidden

- Do not change benchmark code, backend code, safe-summary code, tests, schema,
  digest, workload, or any of the six accepted thresholds.
- Do not skip, conditionally exclude, or retry-until-green the real benchmark.
- Do not treat a CPU-throttled local run as valid immutable performance
  evidence.
- Do not add Phase 1 telemetry, public API, frontend, or product behavior.

## Acceptance Criteria

- [ ] RESULT describes the schema-v3 matrix in historical order and points to
      the recorded successful final run.
- [ ] PERF-001 no longer says completion remains gated after the matrix passed.
- [ ] FSQ-0001 records verifier disposition and the exact unchanged digest,
      candidate SHA, and green push/PR matrix runs.
- [ ] Git proves benchmark/backend/safe-summary/tests and all six thresholds
      are unchanged by this task.
- [ ] `make test-trial005`, `make check`, and `make gate-phase4` pass.

## No-Test Reason

This task changes evidence records only; existing benchmark and gate commands
verify that the immutable workload and thresholds remain unchanged.

## Verification

Run:

```sh
make test-trial005
make check
make gate-phase4
git diff --check
```

Expected result:

```text
schema-v3 evidence is consistent and every existing tracepoint gate passes
```

## Risks

- Rewording history could accidentally imply the old wall-clock matrix proves
  schema v3.
- Closing FSQ-0001 without exact immutable evidence could hide a regression.
- Touching the benchmark would require a new digest and is outside this task.

## Reviewer Focus

- Does every final-matrix claim identify the correct schema, SHA, and run?
- Is FSQ-0001's original ratio signature kept distinct from the invalid local
  absolute-budget sample under CPU throttling?
- Is this diff evidence-only with byte-identical benchmark and thresholds?

## Role Outputs

Implementer:
- Pending.

Adversarial Reviewer:
- Reviewer 1: Pending.
- Reviewer 2: waived: the independent Phase 4 diagnostic reviewer and final
  integration reviewer cover this evidence-only correction.

Fixer:
- Pending.

Quality Governor:
- Pending.

## Verifier Evidence

- Command: Pending.
- Result: Pending.
- Notes: Pending.

## Failure Queue Items

- FSQ-0001
