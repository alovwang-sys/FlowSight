# Task: Implement SQLite WAL Writer Trial

## Task Metadata

```yaml
task_id: TRIAL-002
release: v1
task_type: implementation
status: planned
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
- `pyproject.toml`
- `Makefile`

## Expected Changed Files

- `flowsight/store/writer.py`
- `tests/store/test_wal_writer.py`

## Forbidden

- Do not add OTel ingest.
- Do not add query API endpoints.
- Do not add UI code.
- Do not store raw Python objects.

## Acceptance Criteria

- [ ] SQLite WAL mode is enabled.
- [ ] Writes go through one bounded serialized queue with one proven writer identity.
- [ ] Concurrent producers can queue and flush 100 fake events with no loss or duplicate.
- [ ] Close/enqueue races and shutdown flush are covered without sleeps.
- [ ] Storage errors are visible, not swallowed.

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

## Reviewer Focus

- Are writes actually serialized?
- Can queued events be lost on shutdown?
- Did this accidentally start Phase 1 ingest?

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
