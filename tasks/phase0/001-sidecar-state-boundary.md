# Task: Promote the Sidecar State Boundary

## Task Metadata

```yaml
task_id: P0-001
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [TRIAL-001, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-001

## Phase

Phase 0

## Goal

Provide the production project-scoped discovery record and private filesystem
boundary that later sidecar election and SDK attachment can trust.

## Context

- TRIAL-004 proved the state/lock architecture, but production code must live
  under `flowsight/` and may not import the spike package.
- This slice deliberately stops before file locking, process launch, HTTP
  listening, SQLite ownership, or OTel integration.
- Source of truth: MVP design sections 4.2, 4.4, 7.4, and Phase 0 in section 11.

## Related Fact IDs

- FS-001
- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/state.py`
- `tests/sidecar/test_state.py`
- `Makefile`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/state.py`
- `tests/sidecar/test_state.py`
- `Makefile`

## Forbidden

- Do not import production behavior from `spikes/sidecar_otel`.
- Do not start a listener, child process, thread, queue, or SQLite connection.
- Do not add OTel, ingest, trace, tracepoint, or UI behavior.
- Do not add Windows or free-threaded compatibility claims.
- Do not expose capability tokens through repr, errors, or permissive file modes.

## Acceptance Criteria

- [ ] The exact versioned state schema accepts only `127.0.0.1`, valid bounded
  built-in fields, and the supported protocol/state versions.
- [ ] Capability tokens are omitted from repr, while wire round-trips preserve
  the exact authenticated discovery record.
- [ ] Runtime directories are mode `0700`; lock/state/database paths are
  project-scoped; an atomically published state file is mode `0600` and durable.
- [ ] Missing, symlink/non-regular, insecure-mode, oversized, malformed, and
  schema-invalid state records fail closed without leaking raw content.
- [ ] Ownership-aware removal cannot delete a successor's state record.
- [ ] `make test-phase0` runs the current production Phase 0 tests, and full
  repository checks pass.

## No-Test Reason

N/A

## Verification

Run:

```sh
make test-phase0
make check
```

Expected result:

```text
production Phase 0 state and existing SDK/security/store tests pass
```

## Risks

- Filesystem checks can become time-of-check/time-of-use vulnerabilities if
  validation follows paths or validates a different inode than the one read.
- Atomic replace without file and directory fsync can publish a state record
  that is not durable across a crash.
- An ownership-blind unlink can remove a newly published successor state.

## Reviewer Focus

- Are `O_NOFOLLOW`, regular-file, exact-mode, size, and strict-schema checks
  applied to the opened file descriptor?
- Can any error or representation reveal the token or raw malformed content?
- Can cleanup remove state whose startup identity changed after the read?
- Does this remain a filesystem boundary rather than hidden sidecar lifecycle?

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

- Command: pending
- Result: pending
- Notes: first sustained Phase 0 product slice after all risk gates opened

## Failure Queue Items

- none
