# Task: Promote the Sidecar State Boundary

## Task Metadata

```yaml
task_id: P0-001
release: v1
task_type: implementation
status: review
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
- This slice deliberately stops before sidecar election and its long-lived
  owner lock, process launch, HTTP listening, SQLite ownership, or OTel
  integration. A short non-blocking state-mutation lock is part of the atomic
  publish/compare/remove boundary; it never elects or represents the sidecar
  owner.
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
- Production code must not start a listener, child process, thread, queue, or
  SQLite connection. Tests may use bounded helper threads/subprocesses only to
  verify filesystem atomicity and cross-process lock behavior; they must not
  implement lifecycle or election.
- Do not add OTel, ingest, trace, tracepoint, or UI behavior.
- Do not add Windows or free-threaded compatibility claims.
- Do not expose capability tokens through repr, errors, or permissive file modes.

## Acceptance Criteria

- [x] The exact versioned state schema accepts only `127.0.0.1`, valid bounded
  built-in fields, and the supported protocol/state versions.
- [x] Capability tokens are omitted from repr, while wire round-trips preserve
  the exact authenticated discovery record.
- [x] Runtime directories are mode `0700`; lock/state/database paths are
  project-scoped; an atomically published state file is mode `0600` and durable.
- [x] Missing, symlink/non-regular, insecure-mode, oversized, malformed, and
  schema-invalid state records fail closed without leaking raw content.
- [x] Ownership-aware removal cannot delete a successor published through the
  same cooperating `StateStore` mutation protocol.
- [x] `make test-phase0` runs the current production Phase 0 tests, and full
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
- Primary Codex agent: promoted a strict versioned `SidecarState` and a
  descriptor-anchored, project-scoped `StateStore` with private permissions,
  bounded fail-closed reads, durable atomic publication, and cooperative
  ownership-aware removal. Added the honest `make test-phase0` target and 80
  focused state-boundary tests.

Adversarial Reviewer:
- Reviewer 1: independent filesystem/security review found and replayed path
  swap, macOS alias, durability-race, cleanup/token-chain, and descriptor-reuse
  defects. All accepted findings received regression tests; the final review
  reported no P0/P1/P2 findings with 80 focused tests green on 3.12/3.13.
- Reviewer 2: independent test-evidence review required a second real
  subprocess retry after lock release plus runtime-root replacement, cleanup
  recovery, and exact field-limit coverage. The final review confirmed those
  tests genuinely hit their contracts and reported no P0/P1/P2 findings.

Fixer:
- Primary Codex agent: applied only accepted findings, including canonical
  ancestor anchoring, child-before-parent fsync for ordinary and raced creates,
  single-close descriptor ownership, sanitized cleanup failures, and preserved
  `KeyboardInterrupt`/`SystemExit` propagation.

Quality Governor:
- Independent governor: confirmed the implementation stays inside the Phase 0
  filesystem/state slice and allowlist; production starts no lifecycle,
  election, listener, thread/process, queue, SQLite, or OTel behavior. The task
  wording now explicitly permits bounded test helpers without widening product
  scope.

## Verifier Evidence

- Command: `make test-phase0`; Python 3.12 equivalent; `make gate-phase0`;
  focused Ruff/mypy/state tests; `git diff --check`
- Result: local candidate passed: 80 focused state tests and 177 Phase 0 slice
  tests on macOS CPython 3.12 and 3.13; `make gate-phase0` passed with all 302
  repository tests, format, lint, type, and agent-system checks green.
- Notes: candidate SHA and Ubuntu/macOS x CPython 3.12/3.13 CI are pending. This
  proves only the P0-001 state/filesystem slice; it is not complete Phase 0
  lifecycle, election, SQLite ownership, bundled-UI, or release acceptance.

## Failure Queue Items

- none; current-candidate four-job CI remains pending external evidence
