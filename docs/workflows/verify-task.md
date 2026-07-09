# FlowSight Verify Task

Use this command when acting as the verifier for a task.

## Inputs

- Task card path.
- Claimed verification command.
- Changed files.

## Procedure

1. Read the task card.
2. Confirm the command matches the task scope.
3. Run the narrowest relevant command first.
4. If available and appropriate, run `make check`.
5. Capture exact pass/fail results.
6. Convert any failure into a failure queue item format from `docs/agent-failure-queue.md`.
7. If the task claims to open a gate, run the matching `make gate-phase*` command and record its result separately from `make check`.

## Output Format

```md
## Commands Run

- `<command>`: passed/failed

## Evidence

- <short result summary>

## Failures Queued

- <FSQ item or none>

## Skipped Checks

- <check and reason>
```
