# Task: Spike Tracepoint Backend Go/No-Go

## Task Metadata

```yaml
task_id: TRIAL-005
release: v1
task_type: spike
status: planned
primary_phase: phase4
impacted_phases: []
depends_on: [TRIAL-003]
requires_gates: []
opens_gates: [phase4-tracepoint]
spike_decision: pending
scope_reductions: pending
scope_override: none
scope_override_approved_by: none
```

## Task ID

TRIAL-005

## Phase

Phase 4

## Goal

Determine which tracepoint backend and function shapes can satisfy before-line, named-variable-only capture without global tracing, async cross-request leakage, or interference with existing tracers.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 5.5 Tracepoint
  - 6.4 Safe summaries
  - 6.5 Tracepoint backend risk gate

## Related Fact IDs

- FS-015
- FS-016
- FS-017
- FS-020
- FS-033

## Allowed Files

- `spikes/tracepoint_backend/**`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`
- `docs/agent-facts.tsv`
- `pyproject.toml`
- `Makefile`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `spikes/tracepoint_backend/`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`
- `docs/agent-facts.tsv`

## Forbidden

- Do not implement the production tracepoint UI/API.
- Do not enable interpreter-wide global tracing.
- Do not evaluate user expressions or capture unspecified locals.
- Do not describe a presumed fallback as supported without evidence.

## Acceptance Criteria

- [ ] Standard GIL-enabled CPython 3.12/3.13 sync, async/await, thread-pool, nested-call, concurrent-request, exception, and cancellation cases are exercised; free-threaded builds remain unsupported.
- [ ] Tests prove before-line semantics, named-vars-only capture, and no capture from unrelated functions or requests.
- [ ] Existing debugger/coverage tracer, enable/disable, cleanup, and monitoring tool-ID conflicts are tested or explicitly rejected with evidence.
- [ ] `sys.monitoring` frame-locals feasibility and any scoped `sys.settrace` isolation are measured with a frozen overhead harness.
- [ ] Result is exactly a supported backend, a documented narrowed subset, or Phase 4 no-go; the design/facts are updated accordingly.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/spikes/test_tracepoint_backend.py
make check
```

Expected result:

```text
all backend spike tests pass and a reviewed support-matrix decision is recorded
```

## Risks

- Thread-level tracing may leak across async tasks even when callbacks filter frames.
- A monitoring callback may discover the wrong frame or depend on unstable interpreter behavior.
- Benchmarks may hide debugger/coverage interference.

## Reviewer Focus

- Does a tracer survive `await`, cancellation, exception, or shutdown?
- Are unsupported combinations rejected at configuration time?
- Is the conclusion narrower than the evidence rather than broader?

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

- Command: `python -m pytest tests/spikes/test_tracepoint_backend.py && make check`
- Result: TBD
- Notes: support-matrix decision not yet recorded

## Failure Queue Items

- none
