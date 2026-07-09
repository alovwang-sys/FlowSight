# FlowSight Agent Task Template

Copy this template for every implementation task. Keep it short enough that an implementer, reviewer, fixer, and verifier can all reason about the same boundary.

````md
# Task: <short imperative title>

## Task Metadata

```yaml
task_id: <TASK-ID>
release: v1
task_type: implementation  # implementation | spike | safety | tooling | docs
status: planned             # planned | in_progress | review | blocked | complete
primary_phase: phase0       # exactly one of phase0..phase6
impacted_phases: []
depends_on: []
requires_gates: []       # gates that must already be open before this task starts
opens_gates: []          # fixed gates that require this task to be complete
scope_override: none
scope_override_approved_by: none
```

## Task ID

<TASK-ID>

## Phase

Phase <0-6>

## Goal

<One concrete behavior or contract this task adds or fixes.>

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - <section numbers>

## Related Fact IDs

- FS-<id>
- FS-<id>

## Allowed Files

- `<path>`
- `<path>`

The current task card and its verifier evidence are always writable control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `<path>`
- `<path>`

## Forbidden

- Do not change unrelated modules.
- Do not introduce v1 non-goals.
- Do not weaken or delete existing tests.
- <task-specific forbidden behavior>

## Acceptance Criteria

- [ ] <observable condition>
- [ ] <observable condition>
- [ ] <test or API behavior>

## No-Test Reason

<Use `N/A` unless this task genuinely cannot have automated tests.>

## Verification

Run:

```sh
<command>
```

Expected result:

```text
<what passing looks like>
```

## Risks

- <technical or product risk>

## Reviewer Focus

- <what the adversarial reviewer should attack first>

## Role Outputs

Implementer:
- <summary or link>

Adversarial Reviewer:
- Reviewer 1: <findings or none>
- Reviewer 2: <findings or `waived: reason`>

Fixer:
- <accepted/rejected findings and changes>

Quality Governor:
- <process drift findings or none>

## Verifier Evidence

- Command:
- Result:
- Notes:

## Failure Queue Items

- <queue/failures.jsonl task IDs or none>
````

`status: complete` is valid only when every acceptance box is checked, dependencies are complete, every Role Output contains concrete content, and Verifier Evidence records a real command, passing result, and notes. Reviewer 2 may be omitted only as `waived: <reason>`. After a task enters `in_progress`, agents may update status/evidence/role outputs, but changing Goal, Allowed Files, Acceptance Criteria, or Scope Override requires explicit user or task-owner approval.

`requires_gates` contains already-open phase gates needed to start a downstream task. `opens_gates` is membership in a version-controlled opener set: each listed gate requires this task to be complete. A spike opener must decide `go` or `go-with-scope-reductions`; `no-go` completes the research task but keeps the capability gate closed. `go-with-scope-reductions` must record the concrete reduction as an explicitly approved scope override. A technical gate also stays closed until every fact in its canonical gate-fact set has `command:` verification and concrete existing test paths; `manual:` cannot open it. Ordinary prerequisites between tasks still belong in `depends_on`.

## Example

````md
# Task: Add SQLite TraceRepository insert/query

## Task Metadata

```yaml
task_id: TRIAL-002
release: v1
task_type: implementation
status: planned
primary_phase: phase0
impacted_phases: []
depends_on: []
requires_gates: []
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

TRIAL-002

## Phase

Phase 0

## Goal

Persist Trace records in SQLite and query recent traces in descending start time order.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
- Related design sections:
  - 5.3 Trace
  - 8.1 Storage choice
  - 8.3 Main queries

## Related Fact IDs

- FS-009
- FS-010

## Allowed Files

- `flowsight/store/*`
- `tests/store/*`

## Expected Changed Files

- `flowsight/store/repository.py`
- `tests/store/test_trace_repository.py`

## Forbidden

- Do not add OTel ingest.
- Do not add UI code.
- Do not store snapshots or spans in this task.
- Do not introduce another database.

## Acceptance Criteria

- [ ] SQLite schema includes the Trace fields from the MVP design.
- [ ] `insert_trace` stores one trace.
- [ ] `list_recent_traces(limit)` returns newest first.
- [ ] Tests cover empty DB, one trace, and multiple traces.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/store/test_trace_repository.py
```

Expected result:

```text
all tests passed
```

## Risks

- Timestamp ordering should be deterministic in tests.
- Schema should leave room for later RuntimeSpan tables.

## Reviewer Focus

- Is this accidentally implementing Phase 1 ingest?
- Does the repository hide SQLite details without inventing a large abstraction?

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

- Command: `pytest tests/store/test_trace_repository.py`
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
````
