# Task: Implement SQLite WAL Writer Trial

## Task Metadata

```yaml
task_id: TRIAL-002
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [TRIAL-001]
requires_gates: []
opens_gates: [phase0-sustained]
scope_override: none
scope_override_approved_by: none
```

## Task ID

TRIAL-002

## Phase

Phase 0

## Goal

Implement a small single-writer SQLite queue for fake events so FlowSight has a verified storage/process-model slice before real ingest work.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 4.2 runtime process model
  - 8.1 storage choice
  - 11 Phase 0

## Related Fact IDs

- FS-009
- FS-010

## Allowed Files

- `flowsight/store/**`
- `tests/store/**`
- `docs/agent-facts.tsv`
- `pyproject.toml`
- `Makefile`

## Expected Changed Files

- `flowsight/store/writer.py`
- `flowsight/store/__init__.py`
- `tests/store/test_wal_writer.py`
- `docs/agent-facts.tsv`

## Forbidden

- Do not add OTel ingest.
- Do not add query API endpoints.
- Do not add UI code.
- Do not store raw Python objects.

## Acceptance Criteria

- [x] SQLite WAL mode is enabled.
- [x] SQLite schema versioning exists and rejects databases newer than this writer.
- [x] Within one writer instance, writes go through one bounded serialized queue with one proven writer identity; project-scoped singleton ownership remains TRIAL-004 scope.
- [x] Concurrent producers can queue and flush 100 fake events with no loss or duplicate.
- [x] Close/enqueue races and shutdown flush are covered without sleeps.
- [x] Queue-full and injected storage errors are visible to callers and health state, not swallowed.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/store/test_wal_writer.py
make check
```

Expected result:

```text
targeted WAL writer test and shared checks passed
```

## Risks

- Queue tests may pass without proving serialization.
- Shutdown behavior can be flaky if tests rely on sleeps.
- This trial proves one connection/thread per writer instance; TRIAL-004 proves only one project-scoped sidecar creates that writer.

## Reviewer Focus

- Are writes actually serialized?
- Can queued events be lost on shutdown?
- Did this accidentally start Phase 1 ingest?

## Role Outputs

Implementer:
- Added a synchronous-initializing, bounded single-writer WAL queue with versioned schema validation, immutable health snapshots, fail-stop error reporting, and exact built-in fake-event inputs.

Adversarial Reviewer:
- Reviewer 1: Reproduced late migration after an unreliable startup timeout and malformed-table promotion; fixes removed the unrequired timeout contract and validate exact v0/v1 table shape before startup or promotion.
- Reviewer 2: Found probabilistic close/enqueue and unbounded FULL evidence; fixes added a forced critical-section interleaving, bounded Future completion, and explicit per-instance ownership scope. Re-review found no remaining blocker.

Fixer:
- Applied every accepted finding, added write/commit/rollback/close failure coverage, preserved bounded flush/close semantics, and rejected startup-timeout behavior that Python threads could not safely guarantee.

Quality Governor:
- Approved the five-path Phase 0 slice: all paths match TRIAL-002, FS-009 has command-backed evidence, FS-010 remains deferred to TRIAL-004, and no OTel, API, UI, or tracepoint behavior entered the task.

## Verifier Evidence

- Command: `.venv/bin/python -m pytest tests/store/test_wal_writer.py`; `make check`
- Result: PASS
- Notes: Independent verification of commit `f3f6c0adb69156a09943001279eae071977a0bb5` passed 15 targeted tests and 19 full product tests on CPython 3.13.5; the worktree was clean and the output correctly remained partial-scaffold rather than Phase 0 evidence. `phase0-sustained` remains closed pending TRIAL-004 and FS-007/FS-008/FS-010 evidence.

## Failure Queue Items

- none
