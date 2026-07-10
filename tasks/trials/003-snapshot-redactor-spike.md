# Task: Spike Runtime Safe Summary

## Task Metadata

```yaml
task_id: TRIAL-003
release: v1
task_type: safety
status: in_progress
primary_phase: phase0
impacted_phases: [phase1, phase3, phase4]
depends_on: [TRIAL-001]
requires_gates: []
opens_gates: [phase1-runtime-ingest, phase4-tracepoint]
scope_override: none
scope_override_approved_by: none
```

## Task ID

TRIAL-003

## Phase

Phase 0

## Goal

Implement the shared pre-queue safe summary and redaction primitive for every future runtime payload, without OTel or tracepoint runtime integration.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 5.4 RuntimeSpan
  - 5.6 Snapshot
  - 6.4 safe summaries and variable snapshots
  - 7 security and privacy
  - 11 Phase 0

## Related Fact IDs

- FS-018
- FS-019
- FS-021
- FS-029

## Allowed Files

- `flowsight/security/**`
- `tests/security/**`
- `docs/agent-facts.tsv`
- `pyproject.toml`
- `Makefile`

## Expected Changed Files

- `flowsight/security/safe_summary.py`
- `flowsight/security/__init__.py`
- `tests/security/test_safe_summary.py`
- `docs/agent-facts.tsv`

## Forbidden

- Do not implement runtime tracepoints.
- Do not install OTel or persist real runtime events.
- Do not use global `sys.settrace`.
- Do not evaluate Python expressions.
- Do not store raw objects.
- Do not claim Phase 4 tracepoints are complete.

## Acceptance Criteria

- [ ] Secret-like names/paths cover password/passwd/pwd, token/access_token/refresh_token, secret/key/api_key, cookie/session, and auth/credential shapes; string contents cover bearer/JWT and the MVP-design PII patterns.
- [ ] Query, header, SQL, exception, args, return, span-event, and snapshot payload shapes all pass through the same pre-queue primitive in tests.
- [ ] Depth, element-count, per-value, per-summary and total payload limits are deterministic.
- [ ] Exact built-in primitives/containers are supported; subclasses and unknown objects produce a type-only placeholder.
- [ ] Malicious `__repr__`, property, iterator, `__getattribute__`, and custom serializer fixtures prove user code is not called.
- [ ] Raw objects and unredacted originals do not remain in the returned result or retained state.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/security/test_safe_summary.py
make check
```

Expected result:

```text
targeted safe-summary test and shared checks passed
```

## Risks

- Serializer may trigger object side effects.
- Redaction tests may be too narrow and miss common secret shapes.

## Reviewer Focus

- Does this accidentally store raw objects?
- Does it call properties or methods with side effects?
- Does it overstate tracepoint support?

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

- Command: `make check`
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
