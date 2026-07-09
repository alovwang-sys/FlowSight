# FlowSight Agent Failure Queue

FlowSight should treat compiler, type-checker, test, lint, and CI failures as structured work items. This mirrors the useful part of Bun's rewrite workflow: failures become a queue that agents consume in small, reviewable units.

## Queue Item Schema

Use JSON Lines at `queue/failures.jsonl` when automation exists:

```json
{"task_id":"FSQ-0001","phase":"phase0","kind":"test","command":"pytest tests/store/test_trace_repository.py","failure_signature":"AssertionError: expected newest trace first","owner_area":"store","allowed_files":["flowsight/store/**","tests/store/**"],"repro":"pytest tests/store/test_trace_repository.py -k newest","status":"open"}
```

Required fields:

- `task_id`: stable queue ID.
- `phase`: `phase0` through `phase6`.
- `kind`: `lint`, `typecheck`, `test`, `e2e`, `ci`, `security`, or `docs`.
- `command`: command that reproduces the failure.
- `failure_signature`: short unique failure text.
- `owner_area`: module or product area.
- `allowed_files`: path allowlist for the fixer.
- `repro`: smallest known reproduction command.
- `status`: `open`, `claimed`, `fixed`, `verified`, or `wontfix`.

Optional fields:

- `forbidden_files`
- `suspected_cause`
- `related_fact_ids`
- `reviewer_focus`
- `ci_url`
- `notes`

## Queue Rules

- One failure signature should map to one queue item.
- One queue item should have one allowed-file boundary.
- Status flow is `open -> claimed -> fixed -> verified`. Use `wontfix` only with a reason and evidence.
- Agents may fix the bug class if the same pattern appears in sibling code, but must name every extra file.
- Do not convert a failure into broad refactoring work.
- Do not skip, delete, or weaken a failing test to close the item.
- If the failure exposes a missing project rule, update `AGENTS.md` or `docs/agent-facts.tsv`.

## Manual Queue Format

Before automation exists, a task card can act as a queue item:

```md
# Failure FSQ-0001

Phase: phase0
Kind: test
Command: pytest tests/store/test_trace_repository.py
Failure signature: expected newest trace first
Allowed files:
- flowsight/store/**
- tests/store/**
Related facts:
- FS-009
Acceptance:
- The command passes.
- The ordering behavior is covered by a test.
```

## Queue File Policy

- `queue/failures.jsonl` is append-only unless a verifier is updating the status of an existing item.
- A verifier should add or update queue items when verification fails.
- A fixer may mark an item `fixed` only after applying a patch.
- A verifier, not the fixer, marks an item `verified`.
- Keep failure excerpts short; store long logs as artifacts and link them from `notes`.
