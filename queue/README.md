# FlowSight Failure Queue

Use `queue/failures.jsonl` when automated or CI failures need to become agent-consumable work items.

Failures are appended to `failures.jsonl` as implementation and CI evidence is
collected. See `docs/agent-failure-queue.md` for the schema and status rules.
