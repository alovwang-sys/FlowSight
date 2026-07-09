# FlowSight Fix Task

Use this command when acting as the fixer after adversarial review.

## Inputs

- Task card path.
- Reviewer findings.
- Current diff.

## Procedure

1. Read the task card and related fact IDs.
2. Classify each reviewer finding:
   - accepted
   - rejected with evidence
   - deferred to queue item
3. Apply only accepted findings.
4. Keep edits inside the task allowlist.
5. Do not broaden the task while fixing.
6. Update role outputs in the task card or PR summary.

The current task card is an allowed control-plane record. Updating it does not authorize edits to any other file outside the product-code allowlist.

## Output Format

```md
## Accepted Findings

- <finding and change made>

## Rejected Findings

- <finding and evidence>

## Deferred Queue Items

- <FSQ ID or none>

## Verification Needed

- <command>
```
