# Task: Probe One Exact Sidecar Startup

## Task Metadata

```yaml
task_id: P0-005
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-002, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-005

## Phase

Phase 0

## Goal

Provide one bounded, token-safe, authenticated loopback health probe that
returns true only when a response proves the exact `SidecarState` startup.

## Context

- P0-001 provides the strict authenticated discovery record.
- P0-002 provides the exact `/internal/v1/health` server contract.
- MVP section 4.2 requires attachment to authenticate and verify published
  state before treating that startup as live or entering owner election.
- P0-003 and P0-004 are complete but are not direct dependencies: their
  listener and owner-lock primitives belong to the future winner path.
- This slice promotes only the TRIAL-004 one-shot liveness oracle. It does not
  promote its polling, election, spawning, or lifecycle orchestration.
- Source of truth: MVP design sections 4.2, 7.4, Phase 0, and
  `spikes/sidecar_otel/RESULT.md` promotion requirements.

## Related Fact IDs

- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/health.py`
- `tests/sidecar/test_health.py`

The current task card and its verifier evidence are always writable
control-plane records.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/health.py`
- `tests/sidecar/test_health.py`

## Forbidden

- Do not read, publish, repair, or remove state inside the probe.
- Do not acquire an owner lock, bind a listener, perform election, stale
  recovery, polling, retries, or startup double-check orchestration.
- Do not start Uvicorn, a subprocess, thread, queue, SQLite connection, or SDK
  lifecycle in production. Tests may use one bounded `127.0.0.1:0` helper with
  observable readiness and cleanup.
- Do not add ready pipes, startup ACKs, process termination, producer leases,
  sender/ingest, OTel, tracepoint, or UI behavior.
- Do not follow redirects, accept another host, use PID existence as health,
  or treat HTTP 200 alone as proof.
- Do not log or expose the token, Authorization header, raw response body,
  errno, socket error, or exception context.
- Do not import production behavior from `spikes/sidecar_otel`, add a runtime
  dependency, or generalize this into a private JSON client.

## Acceptance Criteria

- [x] The probe accepts only an exact `SidecarState` and a finite, positive,
  bounded built-in timeout; invalid caller inputs fail before opening a
  connection.
- [x] It makes exactly one loopback request to `GET /internal/v1/health`, with
  exactly one expected `Host`, bearer Authorization, and zero-length body. The
  token never enters the target, body, Origin, stdout, stderr, repr, or errors.
- [x] Global `HTTPConnection` debug settings cannot print the capability token.
  Redirects are not followed and ordinary failure performs no automatic retry.
- [x] Success requires status 200 plus a response no larger than 4096 bytes
  whose exact JSON fields, built-in types, and values match the supplied state:
  status, protocol/state versions, project/startup IDs, PID, host, and port.
- [x] Missing, extra, duplicate, wrong-type, or mismatched identity fields;
  non-object JSON; invalid UTF-8/JSON; non-200 responses; truncated framing;
  oversized responses; refusal; disconnect; and timeout all fail closed.
- [x] Ordinary transport/protocol failures return the fixed non-healthy outcome
  without retaining raw exceptions. `KeyboardInterrupt` and `SystemExit`
  propagate after single-attempt cleanup, with only a fixed cleanup note if
  necessary.
- [x] Connection/response cleanup occurs exactly once on every path; an
  ambiguous resource is never retried after a reported close failure.
- [x] A real bounded loopback test proves the exact request and matching
  response. Tests wait for observable conditions with deadlines, not sleeps.
- [x] Focused tests, `make test-phase0`, and `make check` pass on CPython
  3.12/3.13 and the macOS/Linux CI matrix.

The non-healthy result means only that this exact published startup was not
proven healthy. It never authorizes a new sidecar; only the future owner-lock
election slice can grant startup ownership.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_health.py
make test-phase0
make check
```

Expected result:

```text
exact authenticated health-probe tests and all repository checks pass
```

## Risks

- A state file alone can be stale or point at an unrelated local listener.
- Matching only status or PID permits PID reuse or another local service to be
  mistaken for the exact startup.
- Mutable HTTP debug configuration can print bearer headers.
- Unbounded reads or hidden retries can violate startup deadlines.
- Treating non-healthy as election authority can create a split brain.

## Reviewer Focus

- Can any response that does not prove every exact startup field pass?
- Can the token appear through HTTP debugging or exception chaining?
- Is this exactly one bounded probe with no hidden election or retry policy?
- Are connection and response resources cleaned once under ordinary and
  process-control failures?
- Do tests distinguish non-healthy evidence from permission to launch?

## Role Outputs

Implementer:
- Added the exported one-shot health probe with strict input revalidation, an
  exact token-safe request, one monotonic deadline, bounded HTTP/JSON parsing,
  exact startup identity matching, and single-owner cleanup.

Adversarial Reviewer:
- Reviewer 1: found and closed ambiguous `HTTPResponse`/connection ownership,
  per-operation timeout drift, integer-overflow input, response-header boundary,
  control-byte, and packetization-dependent framing defects; final review
  reported P0=0/P1=0/P2=0.
- Reviewer 2: independently rechecked the final request, parser, schema/type,
  deadline, privacy, process-control, cleanup, and real-loopback matrices and
  found no remaining acceptance or scope gap.

Fixer:
- Applied every accepted finding: moved to one direct-socket owner with a total
  deadline, made `Content-Length` framing deterministic, hardened malformed
  header handling and forged-state validation, and added regression tests for
  every corrected boundary. No finding was deferred.

Quality Governor:
- Final review reported P0=0/P1=0/P2=0. Candidate `3573a5e` changes exactly the
  three allowlisted files and remains Phase 0-only: production performs one
  bounded authenticated health probe and introduces no state I/O, listener
  ownership, runtime/spawn, polling, election, SDK lifecycle, queue, SQLite, or
  spike dependency. The sustained gate passes and the supported CI matrix is
  green.

## Verifier Evidence

- Command: focused health-probe tests; `make test-phase0`; `make check`;
  `make gate-phase0`; pre-commit `make check-fast`; candidate GitHub Actions
  matrix
- Result: passed
- Notes: focused tests passed 158/158 on local CPython 3.13; `make test-phase0`
  passed 594 tests; `make check` and `make gate-phase0` each passed 719 tests
  plus formatting, lint, typing, agent checks, and the sustained gate. Candidate
  `3573a5ee529b8c39dfa54b9f3bfa9a720b8168aa` passed
  [run 29144947589](https://github.com/alovwang-sys/FlowSight/actions/runs/29144947589):
  macOS 3.13 job `86524788132`, macOS 3.12 job `86524788143`, Ubuntu 3.13 job
  `86524788157`, and Ubuntu 3.12 job `86524788192`. The full check correctly
  retains the partial-scaffold limitation. This evidence proves only one
  authenticated exact-startup health probe; it does not prove state discovery,
  election authority, sidecar launch/readiness, or complete Phase 0 acceptance.

## Failure Queue Items

- none
