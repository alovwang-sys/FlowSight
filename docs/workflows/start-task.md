# Start FlowSight Task

Use this command when implementing one FlowSight task card.

## Required Inputs

- Task card path.
- Release, task type, status, and exactly one primary phase.
- Dependencies, `requires_gates`, and `opens_gates`.
- Expected verification command.

## Procedure

1. Read `AGENTS.md`.
2. Read `docs/agent-operating-system.md`.
3. Read `docs/agent-facts.tsv` and note related fact IDs.
4. Read the task card.
5. State:
   - Release, primary phase, impacted phases, and task type.
   - Dependency/gate state.
   - Allowed files.
   - Forbidden behavior.
   - Verification command.
6. Implement the smallest slice that satisfies the task.
7. Add or update tests in the allowed scope.
8. Run the narrowest verification command.
9. Summarize:
   - Files changed.
   - Tests run.
   - Related fact IDs preserved.
   - Known follow-up tasks.

## Hard Stops

Stop and ask for direction if:

- The task crosses phases.
- A dependency is not complete or a gate listed in `requires_gates` is closed. Gates listed only in `opens_gates` are outputs of the task and do not block starting it.
- The task requires files outside its allowlist.
- The task needs a v1 non-goal.
- The verification command does not exist and adding it is outside scope.
