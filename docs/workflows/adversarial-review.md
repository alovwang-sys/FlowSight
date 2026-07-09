# FlowSight Adversarial Review

Use this command to review a completed task diff. Assume the implementation is wrong until proven otherwise.

## Inputs

- Task card path.
- Diff or changed files.
- Verification output, if available.

## Procedure

1. Read `AGENTS.md`.
2. Read `docs/agent-operating-system.md`.
3. Read `docs/agent-facts.tsv`.
4. Read the task card.
5. Inspect the complete diff, directly related tests, public-contract consumers, migrations/configuration, and the minimum surrounding code needed to detect integration breakage. This expands read scope, not edit scope.
6. Report findings ordered by severity.

## Review Questions

- Does the diff violate any v1 non-goal?
- Does it cross phases?
- Does it change files outside the task allowlist?
- Are tests capable of failing for the intended bug or behavior?
- Did it weaken, skip, or delete a safety check?
- Does it store raw objects, leak secrets, bind remotely, or enable global tracing?
- Does it invent a broad abstraction where a narrow implementation would do?
- Are TODOs precise enough to become queue items?
- Are dependencies/gates actually complete, and is a bootstrap check being overstated as product evidence?

## Output Format

```md
## Findings

- [P0/P1/P2] <file:line> <issue>

## Open Questions

- <question>

## Verification Gaps

- <missing or untrusted check>

## Bun-Style Process Gaps

- <task/review/queue/rule issue>
```
