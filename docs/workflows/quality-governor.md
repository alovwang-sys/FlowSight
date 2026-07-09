# FlowSight Quality Governor

Use this command to audit the process around a task, not just the code.

## Inputs

- Task card path.
- Diff or changed files.
- Reviewer/fixer/verifier summaries.

## Procedure

1. Read `AGENTS.md`.
2. Read `docs/agent-operating-system.md`.
3. Read `docs/agent-facts.tsv`.
4. Check whether the task followed the agent operating system.
5. Report only process defects that would make future agent work less reliable.

## Review Questions

- Does the task name one phase?
- Are release, task type, status, dependencies, gates, and any scope override valid?
- Are all dependencies complete and does the claimed gate evidence exist?
- Are related fact IDs listed and preserved?
- Did implementation stay inside allowed files?
- Were verification commands real and run?
- Were failures converted into queue items?
- Did a repeated issue reveal a missing rule?
- Did any agent overstate support or completion?
- Is a bootstrap `make check` being misreported as product or phase acceptance?

## Output Format

```md
## Process Findings

- [P0/P1/P2] <issue>

## Required Rule Updates

- <AGENTS/facts/task/hook update or none>

## Approval

- approved / blocked
```
