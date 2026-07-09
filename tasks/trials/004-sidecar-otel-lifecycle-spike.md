# Task: Spike Sidecar and OTel Lifecycle

## Task Metadata

```yaml
task_id: TRIAL-004
release: v1
task_type: spike
status: planned
primary_phase: phase0
impacted_phases: [phase1, phase3]
depends_on: [TRIAL-001, TRIAL-002, TRIAL-003]
requires_gates: []
opens_gates: [phase0-sustained, phase1-runtime-ingest]
spike_decision: pending
scope_reductions: pending
scope_override: none
scope_override_approved_by: none
```

## Task ID

TRIAL-004

## Phase

Phase 0

## Goal

Produce a reviewed go/no-go decision for the Python SDK plus independent local Python sidecar architecture, including reload ownership, OTel coexistence, private event delivery, and bounded lifecycle.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 2.5 v1 support matrix
  - 4.2 Runtime Process Model
  - 4.3 Internal Event Protocol
  - 6.2 OpenTelemetry ownership contract

## Related Fact IDs

- FS-007
- FS-008
- FS-009
- FS-010
- FS-025
- FS-035
- FS-036

## Allowed Files

- `spikes/sidecar_otel/**`
- `tests/spikes/test_sidecar_otel_lifecycle.py`
- `docs/flowsight-mvp-design.md`
- `docs/agent-facts.tsv`
- `pyproject.toml`
- `Makefile`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `spikes/sidecar_otel/`
- `tests/spikes/test_sidecar_otel_lifecycle.py`
- `docs/flowsight-mvp-design.md`
- `docs/agent-facts.tsv`

## Forbidden

- Do not ship a production Phase 1 ingest implementation from the spike directory.
- Do not implement a generic OTLP receiver.
- Do not silently support multiple workers.
- Do not replace a user's existing OTel provider/exporters.
- Do not weaken local authentication or bounded shutdown requirements.

## Acceptance Criteria

- [ ] Ordinary Uvicorn startup and one real `--reload` attach to the same sidecar PID, port, and SQLite owner.
- [ ] Atomic launch locking, stale-state recovery, duplicate `init_app`, default/explicit port conflicts, producer lease, and unsupported second worker are exercised.
- [ ] Existing and FlowSight-owned OTel providers work with exactly one `FlowSightSpanProcessor`, without provider replacement, duplicate instrumentation/root spans, callback-network I/O, or self-capture recursion.
- [ ] `FlowSight.shutdown()` followed by user spans and FlowSight re-init proves the permanently registered processor becomes no-op, is safely reused, and is never duplicated; user provider shutdown remains user-owned.
- [ ] AlwaysOn, sampled, and non-recording/AlwaysOff sampler cases prove the documented visibility and warning behavior.
- [ ] A synthetic FlowSight function child span uses `record_exception=False` and `set_status_on_exception=False`; an existing exporter receives only code identity/timing/status code and has no exception event/attributes/status description, while args/return and safe exception detail stay in the authenticated private enrichment event.
- [ ] Request `contextvars` plus the bounded span-association map preserve ownership across sync, async, and thread-pool child spans; two concurrent local server spans sharing one upstream OTel trace remain separate `request_trace_id` records and never merge completion/drop state.
- [ ] Startup/manual/background spans without a local server ancestor expire from a bounded orphan buffer into `unscoped_span_count` without creating or damaging a Trace; nested local server spans use the nearest-server boundary.
- [ ] Safe private event delivery, ACK-after-commit, bounded flush, explicit stop/idle shutdown, queue/storage failure, and one-second query visibility are measured.
- [ ] The lifecycle harness passes the repository's macOS/Linux × CPython 3.12/3.13 CI matrix.
- [ ] Result is exactly `go`, `go with listed scope reductions`, or `no-go`, and the design/facts are updated accordingly.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/spikes/test_sidecar_otel_lifecycle.py
make check
```

Expected result:

```text
all lifecycle spike tests pass and a reviewed go/no-go decision is recorded
```

## Risks

- A fake reload test may not exercise actual Uvicorn subprocess replacement.
- A stale PID or state file may be mistaken for a live authenticated sidecar.
- The spike may accidentally become unreviewed production architecture.

## Reviewer Focus

- Is reload implemented as sidecar reconnection rather than impossible cross-process thread reuse?
- Can duplicate sidecars, writers, processors, producers, or orphan processes still occur?
- Are failure and timeout paths observable without blocking the business request?

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

- Command: `python -m pytest tests/spikes/test_sidecar_otel_lifecycle.py && make check`
- Result: TBD
- Notes: go/no-go not yet recorded

## Failure Queue Items

- none
