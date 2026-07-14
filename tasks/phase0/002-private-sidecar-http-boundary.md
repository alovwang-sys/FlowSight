# Task: Promote the Private Sidecar HTTP Boundary

## Task Metadata

```yaml
task_id: P0-002
release: v1
task_type: implementation
status: complete
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
- Do not accept a service, writer, lease, or storage object in the app factory,
  and do not ship a write-probe route.
- Do not import production behavior from `spikes/sidecar_otel`.

## Acceptance Criteria

- [x] An exact `SidecarState` with an ASCII URL-safe bearer token builds a
  docs-disabled FastAPI app without starting runtime infrastructure; invalid
  token shapes fail with a fixed error that does not echo the value.
- [x] Every HTTP request requires exactly one expected Host before dispatch;
  exact `/internal/v1` and `/api/v1` roots plus every descendant also require
  exactly one matching bearer capability token before the first ASGI receive,
  route dispatch, or body validator.
- [x] Every non-safe browser method under `/api/v1` (anything other than
  GET/HEAD/OPTIONS) requires exactly one matching Origin, while internal SDK
  writes do not require a browser Origin.
- [x] Private request bodies are capped at 1 MiB before FastAPI parsing;
  Content-Length accepts only decimal digits and must exactly match received
  bytes; every Transfer-Encoding is rejected; duplicates, overflow, invalid
  ASGI messages, and disconnects do not dispatch and, while send remains
  available, return fixed non-sensitive errors.
- [x] The only shipped route is `GET /internal/v1/health`; its exact keys are
  `status`, protocol/state versions, project/startup IDs, sidecar PID, host, and
  port. Its serialized response remains at most 4 KiB for maximum-length
  multibyte IDs. It contains no token, database/storage/writer, producer/lease,
  or SQLite owner fields, and fixed faults/404/405 emit no CORS allow-origin
  header.
- [x] `make test-phase0` discovers all production sidecar tests, and full
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
- A non-private UI/static path still needs exact Host validation to prevent a
  future DNS-rebinding bypass before the UI obtains its fragment token.
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
- Added an inert docs-disabled FastAPI factory behind a raw ASGI boundary that
  enforces global Host, private bearer capability, browser-write Origin, strict
  request framing, and a pre-parser 1 MiB body cap. The only production route
  is the bounded authenticated health response.

Adversarial Reviewer:
- Reviewer 1: final code review found no P0/P1/P2 issue after accepted fixes for
  global Host enforcement, Transfer-Encoding rejection, token-shape validation,
  tuple response-header sanitization, and replay receive-state preservation.
- Reviewer 2: final test review found no P0/P1/P2 evidence gap after accepted
  additions for inert lifespan execution, exact namespace roots, real
  downstream/parser counters, multibyte health bounds, and raw response-header
  privacy scans.

Fixer:
- Applied every accepted reviewer finding and reran the focused suite after the
  final raw-header privacy assertion; no finding was deferred.

Quality Governor:
- Final review reported P0=0/P1=0/P2=0, confirmed the dirty set is allowlisted,
  the production route set remains health-only, no later-phase behavior was
  imported, and `make test-phase0` honestly discovers all sidecar product tests.

## Verifier Evidence

- Command: focused app tests on CPython 3.12/3.13; `make test-phase0`;
  `make check`; phase0-sustained validator; candidate GitHub Actions matrix
- Result: passed
- Notes: focused app tests passed 67/67 on both Python versions;
  `make test-phase0` passed 244 tests; `make check` passed 369 tests plus
  format, lint, type, frontend, build, and agent checks; the gate validator
  passed. Candidate `36a52467fc0bd3840c8daef5c109c199a9f84574`
  passed [run 29140238771](https://github.com/alovwang-sys/FlowSight/actions/runs/29140238771):
  Ubuntu 3.13 job `86511823219`, macOS 3.13 job `86511823221`, macOS 3.12 job
  `86511823223`, and Ubuntu 3.12 job `86511823225`. `make check` correctly
  reports that the present partial scaffold is not full Phase 0 acceptance
  evidence. This completes only the P0-002 private HTTP application-boundary
  slice; it does not claim complete Phase 0 acceptance.

## Failure Queue Items

- none
