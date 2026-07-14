# Task: Spike Runtime Safe Summary

## Task Metadata

```yaml
task_id: TRIAL-003
release: v1
task_type: safety
status: complete
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

- [x] Secret-like names/paths cover password/passwd/pwd, token/access_token/refresh_token, secret/key/api_key, cookie/session, and auth/credential shapes; string contents cover bearer/JWT and the MVP-design PII patterns.
- [x] Query, header, SQL, exception, args, return, span-event, and snapshot payload shapes all pass through the same pre-queue primitive in tests.
- [x] Depth, element-count, per-value, per-summary and total payload limits are deterministic.
- [x] Exact built-in primitives/containers are supported; subclasses and unknown objects produce a type-only placeholder.
- [x] Malicious `__repr__`, property, iterator, `__getattribute__`, and custom serializer fixtures prove user code is not called.
- [x] Raw objects and unredacted originals do not remain in the returned result or retained state.

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

## Safety Result

- Decision: go.
- Supported boundary: an exact built-in payload dict is converted to an immutable, size-bounded JSON envelope; only exact built-ins are expanded, unknown/subclass values are type-only, and no payload or reachable-object protocol is dispatched.
- This result does not implement or claim OTel ingest, persistence integration, or tracepoints.

## Role Outputs

Implementer:
- Added the shared pre-queue JSON envelope with name/path/content redaction, exact-built-in traversal, fail-open fixed placeholders, cycle handling, and test-frozen limits of depth 3, 50 items per container, 200 total items, 1KB per value, 4KB per top-level summary, and 16KB total.

Adversarial Reviewer:
- Reviewer 1: Reproduced hostile metaclass descriptor execution, sensitive key/type metadata leaks, unbounded type-label allocation, audited `id()` calls, and concurrent-mutation exceptions; all were fixed with cached built-in descriptors, sanitized metadata, identity reference stacks, and fixed fail-open envelopes.
- Reviewer 2: Found empty-signature JWT/JWE and secret-like type-name false negatives plus coupled limit assertions; dedicated compact-token patterns, type-name checks, and isolated exact-boundary tests closed every finding. Re-review approved 78 targeted tests.

Fixer:
- Applied all accepted findings without broadening scope, made content matches redact whole strings, bounded every report and serialized envelope, removed raw references in `finally`, and added regression tests for every discovered attack path.

Quality Governor:
- Approved the four-path Phase 0 safety slice after both blocker fixes: allowed paths only, no OTel/tracepoint/storage integration, command-backed FS-018/FS-019/FS-021/FS-029 evidence, and no new global rule or failure-queue item required.

## Verifier Evidence

- Command: `python -m pytest tests/security/test_safe_summary.py`; `make check`
- Result: PASS
- Notes: Independent verification of commit `01beb2c5191abb045f180563192b02c6f4181976` passed 78 targeted tests and 97 full product tests on CPython 3.13.5; formatting, lint, mypy, and all agent checks passed. Output correctly remained partial-scaffold rather than Phase 0 evidence; phase1 and phase4 gates still require TRIAL-004/TRIAL-005 decisions and their remaining facts.

## Failure Queue Items

- none
