# Task: Promote the Private Sidecar HTTP Boundary

## Task Metadata

```yaml
task_id: P0-002
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-002

## Phase

Phase 0

## Goal

Provide the production ASGI application boundary that authenticates and bounds
all future private sidecar HTTP routes before FastAPI parses request bodies.

## Context

- P0-001 provides the exact project/startup/port/token binding consumed here.
- TRIAL-004 proved the loopback Host/token/Origin policy and a pre-parser body
  cap; this slice promotes only that HTTP application boundary and a safe health
  route.
- The factory returns an ASGI app but does not bind a socket or start sidecar
  lifecycle. Source of truth: MVP design sections 4.2, 4.3, 7.4, and Phase 0.

## Related Fact IDs

- FS-001
- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/app.py`
- `tests/sidecar/test_app.py`
- `Makefile`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/app.py`
- `tests/sidecar/test_app.py`
- `Makefile`

## Forbidden

- Do not bind a socket or start Uvicorn, a child process, a background thread,
  a queue, a SQLite connection, sidecar election, or SDK lifecycle.
- Do not add ingest, producer leases, persistence, query APIs, OTel, tracepoint,
  or UI behavior.
- Do not add a permissive CORS policy or treat loopback as authentication.
- Do not log, return, or expose the capability token or database path.
- Do not import production behavior from `spikes/sidecar_otel`.

## Acceptance Criteria

- [ ] The exact `SidecarState` builds a docs-disabled FastAPI app without
  starting runtime infrastructure.
- [ ] Every `/internal/v1/` and `/api/v1/` request requires exactly one matching
  Host and bearer capability token before route dispatch.
- [ ] Browser write requests under `/api/v1/` also require exactly one matching
  Origin, while internal SDK writes do not require a browser Origin.
- [ ] Private request bodies are capped at 1 MiB before FastAPI parsing;
  duplicate/invalid lengths, overflow, mismatch, invalid ASGI messages, and
  disconnects fail with fixed non-sensitive errors.
- [ ] The authenticated health response contains only bounded public sidecar
  identity/status fields and no token or database path; responses emit no CORS
  allow-origin header.
- [ ] `make test-phase0` discovers all production sidecar tests, and full
  repository checks pass.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_app.py
make test-phase0
make check
```

Expected result:

```text
private ASGI authentication/body-boundary tests and all repository checks pass
```

## Risks

- FastAPI dependencies run after request parsing, so body limits must live at
  the outer ASGI boundary rather than only in route dependencies.
- Duplicate Host, Authorization, Origin, or Content-Length headers can create
  request-smuggling or policy ambiguity if accepted.
- Error paths can accidentally echo a token-bearing header or enable CORS.

## Reviewer Focus

- Can any private path reach FastAPI before Host/token/Origin and body checks?
- Are header multiplicity and comparisons exact and token-safe?
- Can chunking, a lying Content-Length, disconnect, or malformed ASGI message
  bypass the pre-parser 1 MiB bound?
- Does the production factory remain an inert app shell rather than hidden
  lifecycle or storage ownership?

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
- Notes: promotes only the Phase 0 private HTTP application boundary

## Failure Queue Items

- none
